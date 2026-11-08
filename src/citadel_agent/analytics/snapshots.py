# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Versioned analytics generations and same-partition, fenced Cosmos checkpoints."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosBatchOperationError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from pydantic import Field, ValidationError, field_validator, model_validator

from citadel_agent.analytics.pricing import ModelPricingEntry, UsageCost
from citadel_agent.analytics.rollups import FinOpsCheckpoint
from citadel_agent.analytics.usage import UsageObservation
from citadel_agent.azure_clients.cosmos import CosmosQueryPage
from citadel_agent.contracts.common import CitadelModel
from citadel_agent.portal.contracts import HubScope, TimeRange

if TYPE_CHECKING:
    from citadel_agent.portal.product_contracts import PortalHubConfig

logger = logging.getLogger(__name__)
PAGE_SIZE = 20
MAX_BATCH_BYTES = 1_000_000
MAX_GENERATION_WRITE_BYTES = 2_000_000_000
LEASE_SECONDS = 120
SNAPSHOT_LIFETIME = timedelta(days=3)
_HASH_MODULUS = 1 << 256
_Payload = TypeVar("_Payload", bound=CitadelModel)


def _utc(value: datetime | None) -> datetime | None:
    if value is not None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("analytics timestamps must include a timezone")
        return value.astimezone(UTC)
    return value


class AnalyticsUnavailable(RuntimeError):
    """Analytics are not ready, expired or cannot be read safely."""


class AnalyticsConflict(RuntimeError):
    """A competing worker owns the current generation or its checkpoint."""


class AnalyticsLimitExceeded(AnalyticsUnavailable):
    """No partial financial generation may be published after a capacity guard."""


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def source_fingerprint(config: PortalHubConfig) -> str:
    return canonical_hash(
        [
            1,
            config.scope.model_dump(mode="json"),
            config.usage_endpoint.casefold().rstrip("/"),
            config.usage_database,
            config.apim_name,
            config.usage_partition_field,
            config.completeness_container,
        ]
    )


def observation_digest(event: UsageObservation) -> int:
    facts = event.model_dump(mode="json", exclude={"ingested_at", "warning_codes"})
    facts["available_usage_fields"] = sorted(event.available_usage_fields)
    return int(canonical_hash(facts), 16)


def add_digest(current: str, event: UsageObservation) -> str:
    return f"{(int(current, 16) + observation_digest(event)) % _HASH_MODULUS:064x}"


def period_key(window: TimeRange) -> str:
    return canonical_hash(window.model_dump(mode="json"))


def pricing_fingerprint(prices: Sequence[ModelPricingEntry]) -> str:
    return canonical_hash(sorted(price.model_dump_json(by_alias=True) for price in prices))


class PeriodCompletenessProof(CitadelModel):
    """A trusted producer assertion; consumers never infer or write these records."""

    version: Literal[1] = 1
    scope: HubScope
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    pricing_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    window: TimeRange
    complete_through: datetime
    issued_at: datetime
    source_write_cutoff: int = Field(ge=0)
    event_count: int = Field(ge=0)
    event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("complete_through", "issued_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("completeness timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _closed_period(self) -> PeriodCompletenessProof:
        if self.complete_through != self.window.end or self.issued_at < self.window.end:
            raise ValueError("a completeness record must close its exact declared accounting period")
        if self.source_write_cutoff > int(self.issued_at.timestamp()):
            raise ValueError("source write cutoff cannot follow the completeness assertion")
        return self


class PriceSnapshot(CitadelModel):
    prices: list[ModelPricingEntry]
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime
    proofs: list[PeriodCompletenessProof] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SnapshotInfo(CitadelModel):
    generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    window: TimeRange
    source_cutoff: int = Field(ge=0)
    created_at: datetime
    expires_at: datetime
    full_reconciliation_at: datetime
    pricing_snapshot_at: datetime | None = None
    event_count: int = Field(default=0, ge=0)
    rejected_records: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("created_at", "expires_at", "full_reconciliation_at", "pricing_snapshot_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @model_validator(mode="after")
    def _lifetime(self) -> SnapshotInfo:
        if self.expires_at <= self.created_at:
            raise ValueError("snapshot expiry must follow creation")
        return self


class GenerationState(CitadelModel):
    info: SnapshotInfo
    parent: SnapshotInfo | None = None
    phase: Literal["prices", "seed", "source", "stats", "allocate", "remainder", "ready", "failed"] = "prices"
    feed_ranges: list[dict[str, Any]] = Field(default_factory=list)
    range_index: int = Field(default=0, ge=0)
    continuation: str | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    source_documents: int = Field(default=0, ge=0)
    written_bytes: int = Field(default=0, ge=0)
    remainder_index: int = Field(default=0, ge=0)
    failure_code: str | None = None

    @field_validator("lease_until")
    @classmethod
    def _utc_lease(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class AnalyticsHead(CitadelModel):
    active: GenerationState | None = None
    published: SnapshotInfo | None = None
    last_error_code: str | None = None


class EventRecord(CitadelModel):
    observation: UsageObservation | None = None
    cost: UsageCost | None = None
    reason: str | None = None


class PeriodTotals(CitadelModel):
    window: TimeRange
    event_count: int = Field(default=0, ge=0)
    event_digest: str = "0" * 64


class AllocationTotals(CitadelModel):
    price_id: str
    weight: int = Field(default=0, ge=0)
    unavailable: bool = False
    last_event: str | None = None
    floor_amount: Decimal = Field(default=Decimal(0), ge=0)
    finalized: bool = False
    warning_code: str | None = None


class RollupRecord(CitadelModel):
    checkpoint: FinOpsCheckpoint

    @model_validator(mode="after")
    def _one_bucket(self) -> RollupRecord:
        if len(self.checkpoint.buckets) != 1:
            raise ValueError("a persisted rollup must contain exactly one complete attribution bucket")
        return self


class AnalyticsDocument(CitadelModel):
    id: str
    partition_key: str = Field(alias="partitionKey")
    scope: HubScope
    kind: str
    payload: dict[str, Any]
    expires_at: datetime | None = None
    ttl: int = -1


@dataclass(frozen=True)
class AnalyticsVersion:
    document: AnalyticsDocument
    etag: str


class AnalyticsContainer(Protocol):
    def read_item(self, item: str, partition_key: str) -> Mapping[str, Any]: ...
    def create_item(self, body: dict[str, Any]) -> Mapping[str, Any]: ...
    def replace_item(self, item: str, body: dict[str, Any], **kwargs: Any) -> Mapping[str, Any]: ...
    def query_items(self, query: str, **kwargs: Any) -> Any: ...
    def execute_item_batch(self, batch_operations: Sequence[Any], partition_key: str) -> Any: ...


class AnalyticsStore:
    """Only unpublished generation rows are mutable; an ETag fences every data/cursor batch."""

    def __init__(
        self,
        container: AnalyticsContainer,
        scope: HubScope,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.container = container
        self.scope = scope
        self.clock = clock
        self.scope_key = canonical_hash(scope.model_dump(mode="json"))

    def partition(self, generation: str | None = None) -> str:
        if generation is not None and (
            len(generation) != 64 or any(value not in "0123456789abcdef" for value in generation)
        ):
            raise ValueError("generation IDs must be lowercase SHA-256 values")
        return self.scope_key if generation is None else f"{self.scope_key}:{generation}"

    def make(
        self,
        kind: str,
        identity: str,
        payload: CitadelModel,
        *,
        generation: str | None = None,
        expires_at: datetime | None = None,
    ) -> AnalyticsDocument:
        ttl = -1
        if expires_at is not None:
            ttl = max(1, math.ceil((expires_at - self.clock()).total_seconds()) + 60)
        return AnalyticsDocument(
            id=identity,
            partition_key=self.partition(generation),
            scope=self.scope,
            kind=kind,
            payload=payload.model_dump(by_alias=True, mode="json"),
            expires_at=expires_at,
            ttl=ttl,
        )

    def get(self, identity: str, generation: str | None = None) -> AnalyticsVersion:
        raw = self.container.read_item(identity, partition_key=self.partition(generation))
        value = self.decode(raw)
        if value.document.partition_key != self.partition(generation):
            raise KeyError("Analytics record is outside the configured partition.")
        return value

    def maybe_get(self, identity: str, generation: str | None = None) -> AnalyticsVersion | None:
        try:
            return self.get(identity, generation)
        except CosmosResourceNotFoundError:
            return None

    def create(self, document: AnalyticsDocument) -> AnalyticsVersion:
        try:
            self.container.create_item(document.model_dump(by_alias=True, mode="json"))
        except CosmosResourceExistsError:
            return self.get(
                document.id,
                document.partition_key.removeprefix(self.scope_key + ":")
                if document.partition_key != self.scope_key
                else None,
            )
        return self.get(
            document.id,
            document.partition_key.removeprefix(self.scope_key + ":")
            if document.partition_key != self.scope_key
            else None,
        )

    def replace(self, current: AnalyticsVersion, payload: CitadelModel) -> AnalyticsVersion:
        if current.document.kind == "generation":
            prior = self.payload(current, GenerationState)
            if prior.phase in {"ready", "failed"}:
                raise ValueError("terminal analytics generations are immutable")
        elif current.document.kind != "head":
            raise ValueError("analytics data writes require an atomic generation checkpoint")
        document = current.document.model_copy(
            update={"payload": payload.model_dump(by_alias=True, mode="json")}, deep=True
        )
        try:
            self.container.replace_item(
                document.id,
                document.model_dump(by_alias=True, mode="json"),
                partition_key=document.partition_key,
                etag=current.etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code in {409, 412}:
                raise AnalyticsConflict("Analytics state changed; retry its current checkpoint.") from exc
            raise
        return self.get(
            document.id,
            document.partition_key.removeprefix(self.scope_key + ":")
            if document.partition_key != self.scope_key
            else None,
        )

    def commit(
        self,
        current: AnalyticsVersion,
        state: GenerationState,
        records: Sequence[AnalyticsDocument] = (),
    ) -> AnalyticsVersion:
        prior = self.payload(current, GenerationState)
        if (
            current.document.kind != "generation"
            or prior.phase in {"ready", "failed"}
            or (state.phase == "ready" and records)
            or state.info.generation != prior.info.generation
            or state.info.source_fingerprint != prior.info.source_fingerprint
            or state.info.window != prior.info.window
            or state.info.source_cutoff != prior.info.source_cutoff
        ):
            raise ValueError("only a building generation may commit data")
        if len(records) > 98 or len({record.id for record in records}) != len(records):
            raise AnalyticsLimitExceeded("Analytics transactional batch operation limit exceeded.")
        if any(
            record.partition_key != current.document.partition_key
            or record.scope != self.scope
            or record.id == current.document.id
            for record in records
        ):
            raise ValueError("analytics data and its cursor must share the same scoped partition")
        operations: list[Any] = [
            ("upsert", (record.model_dump(by_alias=True, mode="json"),)) for record in records
        ]
        state = state.model_copy(deep=True)
        state.lease_until = self.clock() + timedelta(seconds=LEASE_SECONDS)
        state.written_bytes += (
            len(json.dumps(operations).encode())
            + len(current.document.model_dump_json(by_alias=True).encode())
            + 1000
        )
        if state.written_bytes > MAX_GENERATION_WRITE_BYTES and state.phase != "failed":
            raise AnalyticsLimitExceeded("Analytics generation write budget exceeded.")
        control = current.document.model_copy(
            update={"payload": state.model_dump(by_alias=True, mode="json")}, deep=True
        )
        operations.append(
            (
                "replace",
                (control.id, control.model_dump(by_alias=True, mode="json")),
                {"if_match_etag": current.etag},
            )
        )
        if len(json.dumps(operations).encode()) > MAX_BATCH_BYTES:
            raise AnalyticsLimitExceeded("Analytics transactional batch byte limit exceeded.")
        try:
            self.container.execute_item_batch(operations, partition_key=control.partition_key)
        except (CosmosHttpResponseError, CosmosBatchOperationError) as exc:
            codes = [exc.status_code]
            if isinstance(exc, CosmosBatchOperationError):
                codes.extend(value.get("statusCode") for value in (exc.operation_responses or []))
            if any(code in {409, 412} for code in codes):
                raise AnalyticsConflict("A newer worker owns the analytics checkpoint.") from exc
            raise
        return self.get(control.id, state.info.generation)

    def page(
        self,
        generation: str,
        query: str,
        parameters: Mapping[str, Any],
        *,
        continuation: str | None = None,
    ) -> CosmosQueryPage:
        results = self.container.query_items(
            query=query,
            partition_key=self.partition(generation),
            parameters=[{"name": "@" + name, "value": value} for name, value in parameters.items()],
            max_item_count=PAGE_SIZE,
        )
        pages = results.by_page(continuation_token=continuation)
        items = tuple(next(pages, ()))
        token = pages.continuation_token
        if token is not None and not isinstance(token, str):
            raise AnalyticsUnavailable("Analytics returned an unsupported continuation token.")
        if token is not None and token == continuation:
            raise AnalyticsUnavailable("Analytics pagination did not make progress.")
        for item in items:
            if self.decode(item).document.partition_key != self.partition(generation):
                raise AnalyticsUnavailable("Analytics query returned a different scoped partition.")
        return CosmosQueryPage(items, token or None)

    def decode(self, raw: Mapping[str, Any]) -> AnalyticsVersion:
        try:
            document = AnalyticsDocument.model_validate(
                {name: value for name, value in raw.items() if not name.startswith("_")}
            )
            etag = raw["_etag"]
            if not isinstance(etag, str) or document.scope != self.scope:
                raise ValueError("analytics scope or ETag is invalid")
            return AnalyticsVersion(document, etag)
        except (ValidationError, ValueError, KeyError) as exc:
            logger.error("Stored analytics metadata failed validation (%s).", type(exc).__name__)
            raise AnalyticsUnavailable("Stored analytics metadata is unavailable.") from None

    @staticmethod
    def payload(version: AnalyticsVersion, model: type[_Payload]) -> _Payload:
        try:
            result = model.model_validate(version.document.payload)
            if (
                isinstance(result, EventRecord)
                and result.observation is not None
                and (
                    result.observation.scope != version.document.scope
                    or version.document.id != "e:" + result.observation.event_id
                )
            ):
                raise AnalyticsUnavailable("Stored analytics observation scope/identity is invalid.")
            return result
        except ValidationError:
            logger.error("Stored analytics payload failed validation.")
            raise AnalyticsUnavailable("Stored analytics payload is unavailable.") from None
