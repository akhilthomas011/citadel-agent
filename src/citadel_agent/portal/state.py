# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""ETag-protected, scope-bound product state, independent of container-local disks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from pydantic import Field

from citadel_agent.contracts.common import CitadelModel
from citadel_agent.portal.contracts import HubScope
from citadel_agent.portal.sessions import CosmosContainer, PortalStateConflict, cosmos_model_fields

MAX_DOCUMENT_BYTES = 1_500_000


class ProductRecordTooLarge(PortalStateConflict):
    """A record exceeded the application limit before a Cosmos write was attempted."""


class ProductContainer(CosmosContainer, Protocol):
    def query_items(
        self,
        query: str,
        parameters: list[dict[str, object]],
        *,
        enable_cross_partition_query: bool,
        max_item_count: int,
    ) -> Iterable[Mapping[str, object]]: ...


class ProductDocument(CitadelModel):
    id: str
    kind: str
    key: str
    scope: HubScope
    subject_id: str
    data: dict[str, Any]
    updated_at: datetime
    expires_at: datetime
    ttl: int = Field(ge=1)


@dataclass(frozen=True)
class VersionedDocument:
    record: ProductDocument
    etag: str


class ProductStore:
    """All reads and listings bind the exact deployment-owned scope, never a browser scope."""

    def __init__(self, container: ProductContainer, scope: HubScope, *, retention_days: int = 30) -> None:
        self.container = container
        self.scope = scope
        self.retention_seconds = retention_days * 86400

    def create(self, kind: str, key: str, subject_id: str, data: dict[str, Any]) -> VersionedDocument:
        from datetime import timedelta

        now = datetime.now(UTC)
        record = ProductDocument(
            id=self._id(kind, key),
            kind=kind,
            key=key,
            scope=self.scope,
            subject_id=subject_id,
            data=data,
            updated_at=now,
            expires_at=now + timedelta(seconds=self.retention_seconds),
            ttl=self.retention_seconds,
        )
        body = self._body(record)
        try:
            self.container.create_item(body=body)
        except CosmosResourceExistsError as exc:
            raise PortalStateConflict("This request already exists; reload its status.") from exc
        return self.get(kind, key)

    def get(self, kind: str, key: str, *, subject_id: str | None = None) -> VersionedDocument:
        record_id = self._id(kind, key)
        try:
            raw = self.container.read_item(item=record_id, partition_key=record_id)
        except CosmosResourceNotFoundError as exc:
            raise KeyError("Product record was not found.") from exc
        result = self._version(raw)
        if (
            result.record.kind != kind
            or result.record.key != key
            or (subject_id is not None and result.record.subject_id != subject_id)
        ):
            raise KeyError("Product record was not found.")
        return result

    def replace(self, current: VersionedDocument, data: dict[str, Any]) -> VersionedDocument:
        if current.record.scope != self.scope:
            raise ValueError("state cannot cross hub scopes")
        record = current.record.model_copy(update={"data": data, "updated_at": datetime.now(UTC)})
        remaining = int((record.expires_at - datetime.now(UTC)).total_seconds())
        if remaining < 1:
            raise PortalStateConflict("This record expired; start a new request.")
        record.ttl = remaining
        try:
            self.container.replace_item(
                item=record.id,
                body=self._body(record),
                partition_key=record.id,
                etag=current.etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (CosmosAccessConditionFailedError, CosmosResourceNotFoundError) as exc:
            raise PortalStateConflict("Another request changed this record; reload it.") from exc
        return self.get(record.kind, record.key)

    def list(self, kind: str, *, subject_id: str | None = None, limit: int = 50) -> list[VersionedDocument]:
        if not 1 <= limit <= 100:
            raise ValueError("list limit must be between 1 and 100")
        sql = (
            "SELECT TOP @limit * FROM c WHERE c.kind = @kind AND c.scope.tenantId = @tenant "
            "AND c.scope.hubId = @hub AND c.scope.environment = @environment "
            "AND c.scope.subscriptionId = @subscription AND c.expiresAt > @now"
        )
        values: dict[str, object] = {
            "limit": limit,
            "kind": kind,
            "tenant": self.scope.tenant_id,
            "hub": self.scope.hub_id,
            "environment": self.scope.environment,
            "subscription": self.scope.subscription_id,
            "now": datetime.now(UTC).isoformat(),
        }
        if subject_id is not None:
            sql += " AND c.subjectId = @subject"
            values["subject"] = subject_id
        sql += " ORDER BY c._ts DESC"
        rows = self.container.query_items(
            query=sql,
            parameters=[{"name": f"@{name}", "value": value} for name, value in values.items()],
            enable_cross_partition_query=True,
            max_item_count=limit,
        )
        results: list[VersionedDocument] = []
        for raw in rows:
            result = self._version(raw)
            if result.record.kind != kind or (
                subject_id is not None and result.record.subject_id != subject_id
            ):
                raise RuntimeError("Product query returned an unexpected identity.")
            results.append(result)
            if len(results) == limit:
                break
        return results

    def _version(self, raw: Mapping[str, object]) -> VersionedDocument:
        record = ProductDocument.model_validate(cosmos_model_fields(raw, ProductDocument))
        if record.scope != self.scope or record.id != self._id(record.kind, record.key):
            raise RuntimeError("Product state identity does not match the authorized hub scope.")
        if record.expires_at <= datetime.now(UTC):
            raise KeyError("Product record expired.")
        etag = raw.get("_etag")
        if not isinstance(etag, str):
            raise RuntimeError("Product state is missing its ETag.")
        return VersionedDocument(record, etag)

    def pending_conversations(self, *, limit: int = 10) -> list[VersionedDocument]:
        if not 1 <= limit <= 100:
            raise ValueError("worker batch limit must be between 1 and 100")
        now = datetime.now(UTC).isoformat()
        values = {
            "limit": limit,
            "kind": "conversation",
            "tenant": self.scope.tenant_id,
            "hub": self.scope.hub_id,
            "environment": self.scope.environment,
            "subscription": self.scope.subscription_id,
            "now": now,
        }
        rows = self.container.query_items(
            query=(
                "SELECT TOP @limit * FROM c WHERE c.kind = @kind AND c.scope.tenantId = @tenant "
                "AND c.scope.hubId = @hub AND c.scope.environment = @environment "
                "AND c.scope.subscriptionId = @subscription AND c.expiresAt > @now "
                'AND (c.data.status = "queued" OR (c.data.status = "running" AND c.data.leaseUntil <= @now)) '
                "ORDER BY c._ts ASC"
            ),
            parameters=[{"name": "@" + name, "value": value} for name, value in values.items()],
            enable_cross_partition_query=True,
            max_item_count=limit,
        )
        result = []
        for raw in rows:
            value = self._version(raw)
            if value.record.kind != "conversation":
                raise RuntimeError("The durable worker query returned a different record kind.")
            if value.record.data.get("status") in {"queued", "running"}:
                result.append(value)
            if len(result) == limit:
                break
        return result

    def _id(self, kind: str, key: str) -> str:
        content = json.dumps([self.scope.model_dump(mode="json"), kind, key], sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()

    @staticmethod
    def _body(record: ProductDocument) -> dict[str, Any]:
        body = record.model_dump(by_alias=True, mode="json")
        if len(json.dumps(body).encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise ProductRecordTooLarge(
                "Product state is too large. Narrow the report window/grouping or start a new conversation."
            )
        return body
