# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""ETag-protected application approvals bound to immutable change evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from pydantic import Field, field_validator, model_validator

from citadel_agent.contracts.common import CitadelModel, GuidStr
from citadel_agent.portal.auth import PortalIdentity
from citadel_agent.portal.authz import (
    HubGrant,
    PortalAction,
    PortalAuthorizationError,
    authorize_portal_action,
)
from citadel_agent.portal.contracts import HubScope
from citadel_agent.portal.sessions import (
    CosmosContainer,
    PortalStateConflict,
    cosmos_model_fields,
)

_SHA = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


class ApprovalEvidence(CitadelModel):
    """Version tuple captured at preview time and rechecked before approving."""

    change_hash: str = Field(pattern=_SHA.pattern)
    repository_base_commit: str = Field(pattern=_REVISION.pattern)
    admin_policy_hash: str | None = Field(default=None, pattern=_REVISION.pattern)
    upstream_commit: str = Field(pattern=_REVISION.pattern)
    preview_hash: str = Field(pattern=_SHA.pattern)


class PortalApprovalRecord(CitadelModel):
    """Approval envelope; GitHub and deployment-environment gates remain independent."""

    id: str = Field(pattern=_SHA.pattern)
    kind: Literal["portal-approval"] = "portal-approval"
    change_id: str = Field(pattern=r"^cs-\d{8}-\d{6}-[0-9a-f]{6}$")
    scope: HubScope
    author_id: GuidStr
    evidence: ApprovalEvidence
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime
    expires_at: datetime
    approved_by: GuidStr | None = None
    approved_at: datetime | None = None
    invalidation_code: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$",
    )
    ttl: int = Field(ge=1)

    @field_validator("created_at", "expires_at", "approved_at")
    @classmethod
    def _timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("approval timestamps must include a timezone")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def _status_fields_match(self) -> PortalApprovalRecord:
        if self.created_at >= self.expires_at:
            raise ValueError("approval expiration must follow creation")
        if self.status is ApprovalStatus.APPROVED:
            if self.approved_by is None or self.approved_at is None:
                raise ValueError("approved records require approver and timestamp")
            if self.approved_at < self.created_at or self.approved_at >= self.expires_at:
                raise ValueError("approval timestamp must fall within the approval lifetime")
        elif self.approved_by is not None or self.approved_at is not None:
            raise ValueError("only approved records can contain approver details")
        if self.status is ApprovalStatus.INVALIDATED and self.invalidation_code is None:
            raise ValueError("invalidated records require an invalidation code")
        if self.status is not ApprovalStatus.INVALIDATED and self.invalidation_code is not None:
            raise ValueError("only invalidated records can contain an invalidation code")
        return self


@dataclass(frozen=True)
class VersionedApproval:
    record: PortalApprovalRecord
    etag: str
    created: bool


class PortalApprovalService:
    """Creates and reviews application approvals without bypassing GitHub deployment gates."""

    def __init__(self, container: CosmosContainer) -> None:
        self._container = container

    def create(
        self,
        *,
        change_id: str,
        scope: HubScope,
        author_id: str,
        evidence: ApprovalEvidence,
        now: datetime,
        ttl_seconds: int,
    ) -> VersionedApproval:
        now = _as_utc(now)
        if ttl_seconds < 1:
            raise ValueError("approval retention must be a positive number of seconds")
        if scope.environment.lower() == "prod" and evidence.admin_policy_hash is None:
            raise ValueError("production approvals require a pinned admin policy hash")
        record_id = _approval_id(change_id, scope)
        record = PortalApprovalRecord(
            id=record_id,
            change_id=change_id,
            scope=scope,
            author_id=author_id,
            evidence=evidence,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            ttl=ttl_seconds,
        )
        try:
            self._container.create_item(body=record.model_dump(by_alias=True, mode="json"))
        except CosmosResourceExistsError as exc:
            existing = self.get(record_id)
            if existing.record.evidence != evidence or existing.record.author_id != author_id:
                raise PortalStateConflict("Change approval already exists with different evidence.") from exc
            return VersionedApproval(
                record=existing.record,
                etag=existing.etag,
                created=False,
            )
        created = self.get(record_id)
        return VersionedApproval(record=created.record, etag=created.etag, created=True)

    def get(self, approval_id: str) -> VersionedApproval:
        if not _SHA.fullmatch(approval_id):
            raise ValueError("approval id must be a lowercase SHA-256 digest")
        try:
            document = self._container.read_item(item=approval_id, partition_key=approval_id)
        except CosmosResourceNotFoundError as exc:
            raise KeyError("Portal approval was not found.") from exc
        etag = document.get("_etag")
        if not isinstance(etag, str):
            raise RuntimeError("Cosmos portal approval is missing its ETag.")
        record = PortalApprovalRecord.model_validate(cosmos_model_fields(document, PortalApprovalRecord))
        return VersionedApproval(record=record, etag=etag, created=False)

    def approve(
        self,
        approval_id: str,
        *,
        identity: PortalIdentity,
        grant: HubGrant,
        requested_scope: HubScope,
        current_evidence: ApprovalEvidence,
        now: datetime,
    ) -> VersionedApproval:
        now = _as_utc(now)
        current = self.get(approval_id)
        record = current.record
        authorize_portal_action(identity, grant, requested_scope, PortalAction.CHANGE_APPROVE)
        if record.scope != requested_scope:
            raise PortalAuthorizationError("The approval belongs to a different hub scope.")
        if record.status is not ApprovalStatus.PENDING:
            raise PortalStateConflict(f"Approval is already {record.status.value}.")
        if record.expires_at <= now:
            self._replace(
                current,
                record.model_copy(update={"status": ApprovalStatus.EXPIRED}),
            )
            raise PortalStateConflict("Approval expired; prepare and preview the change again.")
        if requested_scope.environment.lower() == "prod" and identity.subject_id == record.author_id:
            raise PortalAuthorizationError("Production changes require an independent approver.")
        if record.evidence != current_evidence:
            self._replace(
                current,
                record.model_copy(
                    update={
                        "status": ApprovalStatus.INVALIDATED,
                        "invalidation_code": "evidence-changed",
                    }
                ),
            )
            raise PortalStateConflict("Change evidence changed; prepare and preview it again.")

        approved = record.model_copy(
            update={
                "status": ApprovalStatus.APPROVED,
                "approved_by": identity.subject_id,
                "approved_at": now,
            }
        )
        return self._replace(current, approved)

    def _replace(
        self,
        current: VersionedApproval,
        updated: PortalApprovalRecord,
    ) -> VersionedApproval:
        try:
            self._container.replace_item(
                item=current.record.id,
                body=updated.model_dump(by_alias=True, mode="json"),
                partition_key=current.record.id,
                etag=current.etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (CosmosAccessConditionFailedError, CosmosResourceNotFoundError) as exc:
            raise PortalStateConflict("Approval changed; reload it before proceeding.") from exc
        return self.get(current.record.id)


def _approval_id(change_id: str, scope: HubScope) -> str:
    payload = json.dumps(
        {
            "changeId": change_id,
            "tenantId": scope.tenant_id.lower(),
            "subscriptionId": scope.subscription_id.lower(),
            "hubId": scope.hub_id,
            "environment": scope.environment,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("current time must include a timezone")
    return value.astimezone(UTC)
