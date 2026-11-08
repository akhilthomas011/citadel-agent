# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Durable, idempotent portal jobs with ETag-guarded state transitions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from pydantic import Field, field_validator, model_validator

from citadel_agent.contracts.common import CitadelModel, GuidStr
from citadel_agent.portal.contracts import HubScope
from citadel_agent.portal.sessions import (
    CosmosContainer,
    PortalStateConflict,
    cosmos_model_fields,
)


class PortalJobKind(StrEnum):
    ASSISTANT_TURN = "assistant-turn"
    CHANGE_PREVIEW = "change-preview"
    CHANGE_EXECUTION = "change-execution"
    HUB_PROVISIONING = "hub-provisioning"
    ANALYTICS_EXPORT = "analytics-export"


class PortalJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_STATUSES = frozenset({PortalJobStatus.SUCCEEDED, PortalJobStatus.FAILED, PortalJobStatus.CANCELLED})
_TRANSITIONS = {
    PortalJobStatus.QUEUED: frozenset(
        {PortalJobStatus.RUNNING, PortalJobStatus.FAILED, PortalJobStatus.CANCELLED}
    ),
    PortalJobStatus.RUNNING: _TERMINAL_STATUSES,
}
_HASH = re.compile(r"^[0-9a-f]{64}$")


class PortalJobRecord(CitadelModel):
    """Small durable job envelope; results live in scoped stores, never in chat logs."""

    id: str = Field(pattern=_HASH.pattern)
    kind: Literal["portal-job"] = "portal-job"
    job_kind: PortalJobKind
    scope: HubScope
    subject_id: GuidStr
    idempotency_key_hash: str = Field(pattern=_HASH.pattern)
    request_hash: str = Field(pattern=_HASH.pattern)
    status: PortalJobStatus = PortalJobStatus.QUEUED
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_refs: dict[str, str] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    ttl: int = Field(ge=1)

    @field_validator("created_at", "updated_at", "started_at", "completed_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("job timestamps must include a timezone")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def _status_shape(self) -> PortalJobRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at cannot precede created_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at cannot precede created_at")
        if self.status is PortalJobStatus.RUNNING and self.started_at is None:
            raise ValueError("running jobs require started_at")
        if self.status in _TERMINAL_STATUSES and self.completed_at is None:
            raise ValueError("terminal jobs require completed_at")
        if self.status not in _TERMINAL_STATUSES and self.completed_at is not None:
            raise ValueError("nonterminal jobs cannot have completed_at")
        if self.status is PortalJobStatus.FAILED and self.error_code is None:
            raise ValueError("failed jobs require an error code")
        if self.status is not PortalJobStatus.FAILED and self.error_code is not None:
            raise ValueError("only failed jobs can have an error code")
        return self


@dataclass(frozen=True)
class VersionedPortalJob:
    record: PortalJobRecord
    etag: str
    created: bool


class PortalJobStore(Protocol):
    def submit(
        self,
        *,
        job_kind: PortalJobKind,
        scope: HubScope,
        subject_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        ttl_seconds: int,
    ) -> VersionedPortalJob: ...

    def get(self, job_id: str) -> VersionedPortalJob: ...

    def transition(
        self,
        job_id: str,
        *,
        expected_etag: str,
        status: PortalJobStatus,
        now: datetime,
        result_refs: dict[str, str] | None = None,
        error_code: str | None = None,
    ) -> VersionedPortalJob: ...


class CosmosPortalJobStore:
    """Stores jobs in the application Cosmos container and enforces ETag concurrency."""

    def __init__(self, container: CosmosContainer) -> None:
        self._container = container

    def submit(
        self,
        *,
        job_kind: PortalJobKind,
        scope: HubScope,
        subject_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        ttl_seconds: int,
    ) -> VersionedPortalJob:
        now = _as_utc(now)
        if not 16 <= len(idempotency_key) <= 128:
            raise ValueError("idempotency key must be 16 to 128 characters")
        if not _HASH.fullmatch(request_hash):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        if ttl_seconds < 1:
            raise ValueError("job retention must be a positive number of seconds")
        idempotency_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        job_id = _job_id(scope, subject_id, job_kind, idempotency_hash)
        record = PortalJobRecord(
            id=job_id,
            job_kind=job_kind,
            scope=scope,
            subject_id=subject_id,
            idempotency_key_hash=idempotency_hash,
            request_hash=request_hash,
            created_at=now,
            updated_at=now,
            ttl=ttl_seconds,
        )
        try:
            self._container.create_item(body=record.model_dump(by_alias=True, mode="json"))
        except CosmosResourceExistsError as exc:
            existing = self.get(job_id)
            if existing.record.request_hash != request_hash:
                raise PortalStateConflict(
                    "Idempotency key was already used for a different job request."
                ) from exc
            return VersionedPortalJob(record=existing.record, etag=existing.etag, created=False)
        created = self.get(job_id)
        return VersionedPortalJob(record=created.record, etag=created.etag, created=True)

    def get(self, job_id: str) -> VersionedPortalJob:
        if not _HASH.fullmatch(job_id):
            raise ValueError("job id must be a lowercase SHA-256 digest")
        try:
            document = self._container.read_item(item=job_id, partition_key=job_id)
        except CosmosResourceNotFoundError as exc:
            raise KeyError("Portal job was not found.") from exc
        etag = document.get("_etag")
        if not isinstance(etag, str):
            raise RuntimeError("Cosmos portal job is missing its ETag.")
        record = PortalJobRecord.model_validate(cosmos_model_fields(document, PortalJobRecord))
        return VersionedPortalJob(record=record, etag=etag, created=False)

    def transition(
        self,
        job_id: str,
        *,
        expected_etag: str,
        status: PortalJobStatus,
        now: datetime,
        result_refs: dict[str, str] | None = None,
        error_code: str | None = None,
    ) -> VersionedPortalJob:
        current = self.get(job_id)
        if current.etag != expected_etag:
            raise PortalStateConflict("Portal job changed; reload it before updating.")
        if status not in _TRANSITIONS.get(current.record.status, frozenset()):
            raise PortalStateConflict(
                f"Invalid portal job transition: {current.record.status.value} -> {status.value}."
            )
        now = _as_utc(now)
        if now < current.record.updated_at:
            raise ValueError("job state cannot move backwards in time")
        update = {
            "status": status,
            "updated_at": now,
            "started_at": current.record.started_at or (now if status is PortalJobStatus.RUNNING else None),
            "completed_at": now if status in _TERMINAL_STATUSES else None,
            "result_refs": result_refs if result_refs is not None else current.record.result_refs,
            "error_code": error_code,
        }
        payload = current.record.model_dump()
        payload.update(update)
        next_record = PortalJobRecord.model_validate(payload)
        try:
            self._container.replace_item(
                item=job_id,
                body=next_record.model_dump(by_alias=True, mode="json"),
                partition_key=job_id,
                etag=expected_etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (CosmosAccessConditionFailedError, CosmosResourceNotFoundError) as exc:
            raise PortalStateConflict("Portal job changed; reload it before updating.") from exc
        return self.get(job_id)


def _job_id(scope: HubScope, subject_id: str, job_kind: PortalJobKind, idempotency_hash: str) -> str:
    key = "\n".join(
        (
            scope.tenant_id.lower(),
            scope.subscription_id.lower(),
            scope.hub_id.lower(),
            scope.environment.lower(),
            subject_id.lower(),
            job_kind.value,
            idempotency_hash,
        )
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("current time must include a timezone")
    return value.astimezone(UTC)
