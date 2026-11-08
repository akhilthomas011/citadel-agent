# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Browser-safe contracts and trusted connection configuration for the hosted product."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from citadel_agent.analytics.rollups import UsageDimension
from citadel_agent.analytics.views import CUSTOM_DIMENSIONS, FinOpsAnalysis
from citadel_agent.contracts.common import CitadelModel, GuidStr
from citadel_agent.portal.authz import HubGrant
from citadel_agent.portal.contracts import (
    DashboardResponse,
    FinOpsAggregate,
    HubScope,
    MonetaryAmount,
    ObservationState,
    OperationalMetric,
    TimeRange,
    TokenUsage,
)


class PortalHubConfig(CitadelModel):
    """Deployment-owned connections. None of these targets can be supplied by a browser."""

    scope: HubScope
    display_name: str = Field(min_length=1, max_length=100)
    apim_resource_group: str = Field(min_length=1, max_length=90)
    apim_name: str = Field(pattern=r"^[A-Za-z0-9-]{1,50}$")
    gateway_url: str
    contracts_repository_url: str
    contracts_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")
    github_token_secret_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,127}$")
    usage_endpoint: str
    usage_database: str = Field(min_length=1, max_length=128)
    usage_partition_field: str = Field(default="id", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    model_endpoint: str
    model_deployment: str = Field(min_length=1, max_length=100)
    model_api_version: str = Field(default="2024-10-21", pattern=r"^\d{4}-\d{2}-\d{2}(-preview)?$")
    log_analytics_workspace_id: GuidStr | None = None
    initial_grants: list[HubGrant] = Field(default_factory=list, max_length=100)
    max_usage_records: int = Field(default=20_000, ge=100, le=100_000)
    completeness_container: str | None = Field(default=None, min_length=1, max_length=128)
    custom_dimension_labels: dict[Literal["custom_dimension_1", "custom_dimension_2"], str] = Field(
        default_factory=dict
    )
    pipeline_resources: dict[Literal["event_hub", "processor", "usage_store"], str] = Field(
        default_factory=dict
    )

    @field_validator("custom_dimension_labels")
    @classmethod
    def _custom_labels(cls, labels: dict[str, str]) -> dict[str, str]:
        if any(not 1 <= len(value.strip()) <= 80 for value in labels.values()):
            raise ValueError("custom dimension labels must contain 1-80 non-blank characters")
        if len({value.strip().casefold() for value in labels.values()}) != len(labels):
            raise ValueError("custom dimension labels must be distinct")
        return {name: value.strip() for name, value in labels.items()}

    @field_validator("gateway_url", "usage_endpoint", "model_endpoint", "contracts_repository_url")
    @classmethod
    def _https_connection(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("connections must be HTTPS URLs without embedded credentials or query strings")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _trusted_grants(self) -> PortalHubConfig:
        resource_types = {
            "event_hub": ("Microsoft.EventHub/namespaces",),
            "processor": ("Microsoft.Logic/workflows", "Microsoft.Web/sites"),
            "usage_store": ("Microsoft.DocumentDB/databaseAccounts",),
        }
        for kind, resource_id in self.pipeline_resources.items():
            prefix = (
                rf"/subscriptions/{re.escape(self.scope.subscription_id)}/resourceGroups/[^/]+/providers/"
            )
            types = "|".join(re.escape(value) for value in resource_types[kind])
            if not re.fullmatch(prefix + rf"(?:{types})/[A-Za-z0-9._()-]+", resource_id, re.IGNORECASE):
                raise ValueError("pipeline resources must use an allowed type in the configured subscription")
        if any(grant.scope != self.scope for grant in self.initial_grants):
            raise ValueError("initial grants must target the configured hub scope")
        subjects = [grant.subject_id for grant in self.initial_grants]
        if len(subjects) != len(set(subjects)):
            raise ValueError("initial grants must have unique subject IDs")
        repository = urlsplit(self.contracts_repository_url)
        if repository.hostname != "github.com" or len(repository.path.strip("/").split("/")) != 2:
            raise ValueError("hosted contracts repositories must use https://github.com/owner/repository")
        return self

    @property
    def apim_resource_id(self) -> str:
        return (
            f"/subscriptions/{self.scope.subscription_id}/resourceGroups/{self.apim_resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{self.apim_name}"
        )


class DashboardQuery(CitadelModel):
    window: TimeRange
    group_by: UsageDimension = UsageDimension.MODEL
    model: str | None = Field(default=None, min_length=1, max_length=200)
    product: str | None = Field(default=None, min_length=1, max_length=200)
    application: str | None = Field(default=None, min_length=1, max_length=200)
    dimension_filters: dict[UsageDimension, str | None] = Field(default_factory=dict)

    @field_validator("dimension_filters")
    @classmethod
    def _filter_values(cls, values: dict[UsageDimension, str | None]) -> dict[UsageDimension, str | None]:
        if any(
            value is not None and (not value.strip() or not 1 <= len(value) <= 200)
            for value in values.values()
        ):
            raise ValueError(
                "dimension filters must contain 1-200 characters, or null for unattributed usage"
            )
        return values

    def filters(self) -> dict[str, str | None]:
        return {
            **{
                name: value
                for name, value in (
                    ("model", self.model),
                    ("product", self.product),
                    ("application", self.application),
                )
                if value is not None
            },
            **self.dimension_filters,
        }

    def uses_custom_dimensions(self) -> bool:
        return self.group_by in CUSTOM_DIMENSIONS or any(
            value in CUSTOM_DIMENSIONS for value in self.dimension_filters
        )

    @model_validator(mode="after")
    def _bounded(self) -> DashboardQuery:
        if self.window.end - self.window.start > timedelta(days=93):
            raise ValueError("dashboard windows cannot exceed 93 days")
        if self.window.end > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("dashboard windows cannot end in the future")
        return self


class FinOpsResponse(DashboardResponse):
    report_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_scan_complete: bool = False
    input_kind: Literal["source-scan", "read-model"] = "source-scan"
    read_model_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    window: TimeRange
    requests: int = Field(ge=0)
    usage: TokenUsage
    costs: list[MonetaryAmount]
    priced_usage_percent: Decimal = Field(ge=0, le=100)
    unpriced_requests: int = Field(ge=0)
    rejected_records: int = Field(ge=0)
    scanned_records: int = Field(ge=0)
    truncated: bool
    aggregates: list[FinOpsAggregate]
    models: list[str]
    products: list[str]
    applications: list[str]
    filter_options: dict[UsageDimension, list[str]] = Field(default_factory=dict)
    analysis: FinOpsAnalysis | None = None


class ReportStartRequest(CitadelModel):
    request_id: UUID
    query: DashboardQuery
    input_kind: Literal["source-scan", "read-model"] | None = None
    include_analysis: bool = False
    include_custom_dimensions: bool = False
    read_model_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _custom_profiles_need_analysis(self) -> ReportStartRequest:
        if self.include_custom_dimensions and not self.include_analysis:
            raise ValueError("custom profile inclusion requires analytical projections")
        if self.read_model_version is not None and self.input_kind == "source-scan":
            raise ValueError("a maintained generation cannot be attached to a source-scan request")
        return self


class FinOpsReportResponse(CitadelModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: HubScope
    query: DashboardQuery
    include_analysis: bool = False
    custom_dimensions: list[UsageDimension] = Field(default_factory=list)
    input_kind: Literal["source-scan", "read-model"] = "source-scan"
    read_model_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: Literal["pending", "running", "succeeded", "failed", "cancelled"]
    scanned_records: int = Field(ge=0)
    matched_requests: int = Field(ge=0)
    rejected_records: int = Field(ge=0)
    updated_at: datetime
    expires_at: datetime
    pricing_snapshot_at: datetime
    error_code: str | None = None
    result: FinOpsResponse | None = None


class OperationsBucket(CitadelModel):
    timestamp: datetime
    requests: int = Field(ge=0)
    failures: int = Field(ge=0)
    throttled: int = Field(ge=0)
    p95_ms: Decimal | None = Field(default=None, ge=0)


GatewayLogDimension = Literal[
    "backend", "product_id", "subscription_id", "region", "api_id", "operation_name"
]


class OperationsQuery(DashboardQuery):
    gateway_filters: dict[GatewayLogDimension, str | None] = Field(default_factory=dict)

    @field_validator("gateway_filters")
    @classmethod
    def _gateway_filter_values(cls, values: dict[str, str | None]) -> dict[str, str | None]:
        if any(value is not None and (not value.strip() or len(value) > 200) for value in values.values()):
            raise ValueError("gateway filters must be non-blank labels of at most 200 characters")
        return values


class RequestEvidenceQuery(CitadelModel):
    window: TimeRange
    correlation_id: UUID

    @model_validator(mode="after")
    def _bounded_window(self) -> RequestEvidenceQuery:
        DashboardQuery(window=self.window)
        if self.window.end - self.window.start > timedelta(days=7):
            raise ValueError("request evidence windows cannot exceed seven days")
        return self


class RequestEvidence(CitadelModel):
    timestamp: datetime
    correlation_id: str
    response_code: int | None = Field(default=None, ge=100, le=599)
    backend_response_code: int | None = Field(default=None, ge=100, le=599)
    total_ms: Decimal | None = Field(default=None, ge=0)
    backend_ms: Decimal | None = Field(default=None, ge=0)
    backend: str | None = None
    product_id: str | None = None
    subscription_id: str | None = None
    region: str | None = None
    api_id: str | None = None
    operation_name: str | None = None


class RequestEvidenceResponse(DashboardResponse):
    records: list[RequestEvidence]
    truncated: bool = False


class AlertInstance(CitadelModel):
    id: str
    severity: str
    condition: str
    state: str
    started_at: datetime
    modified_at: datetime
    portal_url: str


class AlertInstancesResponse(DashboardResponse):
    alerts: list[AlertInstance]
    truncated: bool = False


class ComponentHealth(CitadelModel):
    component: str
    state: ObservationState
    observed_at: datetime | None = None
    metrics: dict[str, Decimal] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    portal_url: str | None = None


class PipelineHealthResponse(DashboardResponse):
    components: list[ComponentHealth]


class AttributionCount(CitadelModel):
    value: str | None
    requests: int = Field(ge=0)


class OperationsAttribution(DashboardResponse):
    read_model_version: str
    groups: dict[str, list[AttributionCount]]
    truncated: bool = False


class OperationsResponse(DashboardResponse):
    window: TimeRange
    metrics: list[OperationalMetric]
    timeline: list[OperationsBucket]


class ChatMessage(CitadelModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=32_000)
    timestamp: datetime


class ConversationResponse(CitadelModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: HubScope
    title: str
    messages: list[ChatMessage]
    status: Literal["idle", "queued", "running", "failed"]
    error_code: str | None = None
    updated_at: datetime


class TurnRequest(CitadelModel):
    request_id: UUID
    message: str = Field(min_length=1, max_length=8000)

    @field_validator("message")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a message must contain text")
        return value.strip()


class ChangeDecision(CitadelModel):
    approval_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    confirmation: str | None = Field(default=None, max_length=20)


class ChangeResponse(CitadelModel):
    id: str
    scope: HubScope
    author_id: GuidStr
    summary: str
    preview: str
    approval_hash: str
    approval_id: str | None = None
    approval_status: str = "pending"
    execution_status: str = "prepared"
    production: bool
    confirmation_word: str | None = None
    pull_request_url: str | None = None
