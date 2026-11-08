# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Versioned, validated data contracts shared by the portal UI, APIs and analytics."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from citadel_agent.contracts.common import CitadelModel, GuidStr


class PortalRole(StrEnum):
    """Application roles. These are assigned by the identity provider, never by a request body."""

    VIEWER = "Platform.Viewer"
    FINOPS_ANALYST = "Platform.FinOpsAnalyst"
    OPERATOR = "Platform.Operator"
    APPROVER = "Platform.Approver"


class DataSource(StrEnum):
    """Authoritative source family for a returned observation."""

    ARM = "arm"
    APIM = "apim"
    CANONICAL_GIT = "canonical-git"
    COSMOS_USAGE = "cosmos-usage"
    COST_MANAGEMENT = "cost-management"
    LOG_ANALYTICS = "log-analytics"
    APPLICATION_INSIGHTS = "application-insights"


class ObservationState(StrEnum):
    """Health/data state; UNKNOWN is intentionally distinct from healthy and zero."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    STALE = "stale"
    PARTIAL = "partial"


class CostBasis(StrEnum):
    """Financial meaning of an amount. Never aggregate unlike bases into one total."""

    USAGE_ESTIMATE = "usage-estimate"
    ACTUAL = "actual"
    AMORTIZED = "amortized"
    FORECAST = "forecast"


class TimeRange(CitadelModel):
    """A half-open UTC time window [start, end)."""

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _ordered(self) -> TimeRange:
        if self.start >= self.end:
            raise ValueError("time range start must be earlier than end")
        return self


class HubScope(CitadelModel):
    """Tenant and hub/environment scope, always resolved and authorized server-side."""

    tenant_id: GuidStr
    hub_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    environment: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    subscription_id: GuidStr


class DataQuality(CitadelModel):
    """Lineage and completeness metadata required on every analytics response."""

    source: DataSource
    observed_at: datetime
    event_window: TimeRange | None = None
    state: ObservationState
    watermark: datetime | None = None
    warning_codes: list[str] = Field(default_factory=list)

    @field_validator("observed_at", "watermark")
    @classmethod
    def _utc_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps must include a timezone")
            return value.astimezone(UTC)
        return value

    @field_validator("warning_codes")
    @classmethod
    def _unique_warnings(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class CountValue(CitadelModel):
    """A count, or an explicit unknown/unavailable value."""

    value: int | None = Field(default=None, ge=0)
    state: ObservationState
    source: DataSource
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _value_matches_state(self) -> CountValue:
        if self.value is None and self.state in (ObservationState.HEALTHY, ObservationState.DEGRADED):
            raise ValueError("healthy or degraded counts require an observed value")
        if self.value is not None and self.state in (ObservationState.UNAVAILABLE, ObservationState.UNKNOWN):
            raise ValueError("unavailable or unknown counts cannot claim a numeric value")
        return self


class InventorySnapshot(CitadelModel):
    """Configured/observed gateway inventory counts for the Assistant status summary."""

    scope: HubScope
    state: ObservationState
    observed_at: datetime
    backends: CountValue
    model_deployments: CountValue
    access_contracts: CountValue
    mcp_tools: CountValue
    published_agents: CountValue
    active_alerts: CountValue
    warnings: list[str] = Field(default_factory=list)

    @field_validator("observed_at")
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)


class TokenUsage(CitadelModel):
    """Observed token fields. Cached/audio/reasoning values may overlap totals."""

    prompt: int = Field(default=0, ge=0)
    completion: int = Field(default=0, ge=0)
    cached_prompt: int = Field(default=0, ge=0)
    prompt_audio: int = Field(default=0, ge=0)
    completion_audio: int = Field(default=0, ge=0)
    completion_reasoning: int = Field(default=0, ge=0)
    completion_accepted_prediction: int = Field(default=0, ge=0)
    completion_rejected_prediction: int = Field(default=0, ge=0)
    prompt_image: int = Field(default=0, ge=0)
    cached_prompt_image: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)


class UsageDimensions(CitadelModel):
    """Optional attribution dimensions; absent means unavailable, not an inferred value."""

    provider: str | None = None
    model: str | None = None
    deployment: str | None = None
    backend: str | None = None
    product: str | None = None
    application: str | None = None
    region: str | None = None
    service_type: str | None = None
    gateway: str | None = None
    custom_dimension_1: str | None = None
    custom_dimension_2: str | None = None
    operation_name: str | None = None
    ai_gateway_id: str | None = None


class MonetaryAmount(CitadelModel):
    """Non-negative money with explicit currency and accounting basis."""

    amount: Decimal = Field(ge=Decimal("0"), max_digits=50, decimal_places=28)
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    basis: CostBasis
    source: DataSource
    effective_at: datetime | None = None

    @field_validator("effective_at")
    @classmethod
    def _utc_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps must include a timezone")
            return value.astimezone(UTC)
        return value


class PricingCoverage(CitadelModel):
    """How much usage is priced; never imply missing prices are zero cost."""

    priced_usage_percent: Decimal = Field(ge=Decimal("0"), le=Decimal("100"))
    unpriced_dimensions: list[str] = Field(default_factory=list)
    warning_codes: list[str] = Field(default_factory=list)
    pricing_effective_at: datetime | None = None

    @field_validator("pricing_effective_at")
    @classmethod
    def _utc_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps must include a timezone")
            return value.astimezone(UTC)
        return value


class FinOpsAggregate(CitadelModel):
    """A single dimension/time bucket with distinct estimate and billing amounts."""

    scope: HubScope
    window: TimeRange
    dimensions: UsageDimensions
    usage: TokenUsage
    costs: list[MonetaryAmount] = Field(default_factory=list)
    pricing_coverage: PricingCoverage
    quality: DataQuality

    @model_validator(mode="after")
    def _basis_unique_by_currency(self) -> FinOpsAggregate:
        keys = [(cost.basis, cost.currency) for cost in self.costs]
        if len(keys) != len(set(keys)):
            raise ValueError("cost basis/currency pairs must be pre-aggregated")
        return self


class OperationalMetric(CitadelModel):
    """One measured operational signal with units and provenance."""

    name: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._-]+$")]
    value: Decimal | None = None
    unit: Annotated[str, Field(min_length=1, max_length=32)]
    state: ObservationState
    quality: DataQuality

    @model_validator(mode="after")
    def _state_matches_value(self) -> OperationalMetric:
        if self.value is None and self.state in (ObservationState.HEALTHY, ObservationState.DEGRADED):
            raise ValueError("healthy or degraded metrics require an observed value")
        if self.value is not None and self.state in (ObservationState.UNAVAILABLE, ObservationState.UNKNOWN):
            raise ValueError("unavailable or unknown metrics cannot claim a numeric value")
        return self


class DashboardResponse(CitadelModel):
    """Consistent envelope for page-level API responses."""

    schema_version: Literal[1] = 1
    scope: HubScope
    quality: DataQuality
    warnings: list[str] = Field(default_factory=list)
