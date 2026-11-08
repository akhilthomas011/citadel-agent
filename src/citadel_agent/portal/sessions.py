# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Durable, short-lived BFF authentication state stored separately from gateway telemetry."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from pydantic import Field, field_validator, model_validator

from citadel_agent.contracts.common import CitadelModel, GuidStr
from citadel_agent.portal.contracts import PortalRole


class AuthFlowRecord(CitadelModel):
    """One-time MSAL state, nonce and PKCE data with a short server-side lifetime."""

    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["auth-flow"] = "auth-flow"
    flow: dict[str, object]
    expires_at: datetime
    ttl: int = Field(ge=1)

    @field_validator("expires_at")
    @classmethod
    def _expiration_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expiration must include a timezone")
        return value.astimezone(UTC)


class PortalSessionRecord(CitadelModel):
    """Server-side user context; only a hash of the browser's opaque session secret is stored."""

    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["portal-session"] = "portal-session"
    tenant_id: GuidStr
    subject_id: GuidStr
    client_id: str = Field(min_length=1)
    roles: frozenset[PortalRole]
    scopes: frozenset[str]
    csrf_token: str = Field(min_length=32)
    created_at: datetime
    expires_at: datetime
    ttl: int = Field(ge=1)

    @field_validator("created_at", "expires_at")
    @classmethod
    def _timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _timestamps_are_valid(self) -> PortalSessionRecord:
        if self.created_at >= self.expires_at:
            raise ValueError("session expiration must follow creation")
        return self


class PortalSessionStore(Protocol):
    """Persistence contract shared by the BFF and the private agent API."""

    def create_auth_flow(self, record: AuthFlowRecord) -> None: ...

    def consume_auth_flow(self, record_id: str, now: datetime) -> AuthFlowRecord | None: ...

    def create_session(self, record: PortalSessionRecord) -> None: ...

    def get_session(self, record_id: str, now: datetime) -> PortalSessionRecord | None: ...

    def delete_session(self, record_id: str) -> None: ...


class CosmosContainer(Protocol):
    """Subset of the synchronous Cosmos ContainerProxy used by the portal store."""

    def read(self) -> Mapping[str, object]: ...

    def create_item(self, body: Mapping[str, object]) -> object: ...

    def read_item(self, item: str, partition_key: str) -> Mapping[str, object]: ...

    def replace_item(
        self,
        item: str,
        body: dict[str, object],
        partition_key: str,
        *,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
    ) -> object: ...

    def delete_item(
        self,
        item: str,
        partition_key: str,
        *,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
    ) -> object: ...


class PortalStateConflict(RuntimeError):
    """A session or one-time authentication flow identifier was already used."""


class CosmosPortalSessionStore:
    """Persists portal sessions in an application-owned Cosmos container partitioned by /id.

    Configure TTL on the container. Each document also carries its own TTL and is checked
    against its expiration timestamp on read.
    """

    def __init__(self, container: CosmosContainer) -> None:
        self._container = container

    def create_auth_flow(self, record: AuthFlowRecord) -> None:
        self._create(record.model_dump(by_alias=True, mode="json"))

    def consume_auth_flow(self, record_id: str, now: datetime) -> AuthFlowRecord | None:
        now = _as_utc(now)
        raw = self._read(record_id)
        if raw is None:
            return None
        record = AuthFlowRecord.model_validate(cosmos_model_fields(raw, AuthFlowRecord))
        if record.expires_at <= now or not self._delete_if_unchanged(raw):
            return None
        return record

    def create_session(self, record: PortalSessionRecord) -> None:
        self._create(record.model_dump(by_alias=True, mode="json"))

    def get_session(self, record_id: str, now: datetime) -> PortalSessionRecord | None:
        now = _as_utc(now)
        raw = self._read(record_id)
        if raw is None:
            return None
        record = PortalSessionRecord.model_validate(cosmos_model_fields(raw, PortalSessionRecord))
        if record.expires_at <= now:
            self._delete_if_unchanged(raw)
            return None
        return record

    def delete_session(self, record_id: str) -> None:
        try:
            self._container.delete_item(item=record_id, partition_key=record_id)
        except CosmosResourceNotFoundError:
            return

    def _create(self, document: dict[str, object]) -> None:
        try:
            self._container.create_item(body=document)
        except CosmosResourceExistsError as exc:
            raise PortalStateConflict("Portal state identifier already exists.") from exc

    def _read(self, record_id: str) -> Mapping[str, object] | None:
        try:
            return self._container.read_item(item=record_id, partition_key=record_id)
        except CosmosResourceNotFoundError:
            return None

    def _delete_if_unchanged(self, document: Mapping[str, object]) -> bool:
        etag = document.get("_etag")
        record_id = document.get("id")
        if not isinstance(etag, str) or not isinstance(record_id, str):
            raise RuntimeError("Cosmos portal state is missing its id or ETag.")
        try:
            self._container.delete_item(
                item=record_id,
                partition_key=record_id,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (CosmosAccessConditionFailedError, CosmosResourceNotFoundError):
            return False
        return True


def cosmos_model_fields(document: Mapping[str, object], model: type[CitadelModel]) -> dict[str, object]:
    field_names = set(model.model_fields)
    aliases = {
        field.alias
        for field in model.model_fields.values()
        if isinstance(field.alias, str) and field.alias != field.serialization_alias
    }
    aliases.update(
        field.serialization_alias
        for field in model.model_fields.values()
        if isinstance(field.serialization_alias, str)
    )
    return {key: value for key, value in document.items() if key in field_names or key in aliases}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("current time must include a timezone")
    return value.astimezone(UTC)
