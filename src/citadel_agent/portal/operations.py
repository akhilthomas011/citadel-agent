# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Scoped operational evidence, alert instances and explicit component telemetry quality."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from azure.core.exceptions import AzureError
from pydantic import ValidationError

from citadel_agent.analytics.read_model import AnalyticsReader
from citadel_agent.analytics.rollups import UsageDimension
from citadel_agent.analytics.snapshots import AnalyticsUnavailable
from citadel_agent.azure_clients.monitor import AlertReader
from citadel_agent.portal.contracts import DataQuality, DataSource, ObservationState
from citadel_agent.portal.data import PortalDataService
from citadel_agent.portal.product_contracts import (
    AlertInstance,
    AlertInstancesResponse,
    AttributionCount,
    ComponentHealth,
    DashboardQuery,
    OperationsAttribution,
    PipelineHealthResponse,
    RequestEvidence,
    RequestEvidenceQuery,
    RequestEvidenceResponse,
)
from citadel_agent.safety.redaction import default_redactor

logger = logging.getLogger(__name__)
_COMPONENT_METRICS = {
    "event_hub": (
        "IncomingMessages",
        "OutgoingMessages",
        "ServerErrors",
        "UserErrors",
        "ThrottledRequests",
        "Incoming Messages",
        "Outgoing Messages",
        "Server Errors",
        "User Errors",
        "Throttled Requests",
    ),
    "processor": ("RunsStarted", "RunsSucceeded", "RunsFailed", "ActionsFailed", "FunctionExecutionCount"),
    "usage_store": ("TotalRequests", "Total Requests"),
}


def portal_link(resource_id: str) -> str:
    return "https://portal.azure.com/#resource" + quote(resource_id, safe="/")


def _failure(exc: AzureError) -> str:
    status = getattr(exc, "status_code", None)
    return (
        "permission-denied"
        if status in {401, 403}
        else ("rate-limited" if status == 429 else "source-unavailable")
    )


class OperationsService:
    def __init__(
        self,
        data: PortalDataService,
        *,
        analytics: AnalyticsReader | None = None,
        alerts: AlertReader | None = None,
    ) -> None:
        self.data = data
        self.analytics = analytics
        self.alerts = alerts

    def compatibility(self) -> dict[str, Any]:
        from citadel_agent.paths import find_project_root
        from citadel_agent.upstream.drift import check_knowledge, check_property_usage, filter_known
        from citadel_agent.upstream.layout import UpstreamLayout
        from citadel_agent.upstream.lock import read_lock

        try:
            root = find_project_root()
            lock = read_lock(root)
            layout = UpstreamLayout(root / lock.upstream.path)
            raw = check_property_usage(layout) + check_knowledge(layout)
            findings = filter_known(raw)
            state = (
                "difference"
                if any(item.level.value == "error" for item in findings)
                else ("review-required" if findings else "no-detected-schema-drift")
            )
            return {
                "state": state,
                "upstreamCommit": lock.upstream.commit,
                "supportedRelease": lock.upstream.release,
                "knownNoiseCount": len(raw) - len(findings),
                "findings": [
                    {"level": item.level.value, "area": item.area, "message": item.message}
                    for item in findings[:50]
                ],
                "truncated": len(findings) > 50,
                "warnings": [
                    "pinned-schema-and-catalog-check-not-live-deployment-drift",
                    "parameter-template-compilation-is-a-release-validation-step",
                ],
            }
        except (ValueError, RuntimeError, OSError) as exc:
            logger.error("Upstream compatibility evidence is unavailable (%s).", type(exc).__name__)
            return {
                "state": "unavailable",
                "findings": [],
                "warnings": ["compatibility-check-failed-no-clean-drift-claim"],
            }

    def _quality(self, query: DashboardQuery | RequestEvidenceQuery, source=DataSource.LOG_ANALYTICS):
        return DataQuality(
            source=source,
            event_window=query.window,
            observed_at=datetime.now(UTC),
            state=ObservationState.UNAVAILABLE,
        )

    def evidence(self, query: RequestEvidenceQuery) -> RequestEvidenceResponse:
        quality = self._quality(query)
        warnings = ["request-evidence-excludes-bodies-headers-urls-and-addresses"]
        rows = []
        truncated = False
        if self.data.config.log_analytics_workspace_id is None:
            warnings.append("log-analytics-not-configured")
        else:
            fields = (
                "timestamp=TimeGenerated, correlation_id=CorrelationId, response_code=ResponseCode, "
                "backend_response_code=BackendResponseCode, total_ms=TotalTime, backend_ms=BackendTime, "
                "backend=BackendId, product_id=ProductId, subscription_id=ApimSubscriptionId, "
                "region=Region, api_id=ApiId, operation_name=OperationName"
            )
            kql = (
                "ApiManagementGatewayLogs "
                f"| where _ResourceId =~ {json.dumps(self.data.config.apim_resource_id)} "
                f"| where TimeGenerated >= datetime({query.window.start.isoformat()}) "
                f"and TimeGenerated < datetime({query.window.end.isoformat()}) "
                f"| where CorrelationId =~ {json.dumps(str(query.correlation_id))} "
                f"| project {fields} | order by timestamp desc | take 101"
            )
            try:
                result = self.data.logs.query_workspace(
                    self.data.config.log_analytics_workspace_id,
                    kql,
                    (query.window.start, query.window.end),
                )
                truncated = result.partial or len(result.rows) > 100
                for raw in result.rows[:100]:
                    normalized = dict(raw)
                    for field in ("response_code", "backend_response_code"):
                        if normalized.get(field) == 0:
                            normalized[field] = None
                    record = RequestEvidence.model_validate(default_redactor().redact_obj(normalized))
                    if record.correlation_id.casefold() != str(query.correlation_id).casefold():
                        raise ValueError("the evidence source returned a different correlation")
                    if (
                        record.timestamp.tzinfo is None
                        or not query.window.start <= record.timestamp < query.window.end
                    ):
                        raise ValueError("the evidence source returned an invalid timestamp")
                    rows.append(record)
                quality.state = (
                    ObservationState.PARTIAL
                    if truncated
                    else (ObservationState.HEALTHY if rows else ObservationState.UNKNOWN)
                )
                if not rows:
                    warnings.append("no-matching-request-evidence-not-proof-of-success")
                if truncated:
                    warnings.append("request-evidence-partial-or-limit-reached")
            except AzureError as exc:
                logger.warning("Operational request evidence failed (%s).", _failure(exc))
                warnings.append("request-evidence-" + _failure(exc))
            except (ValidationError, ValueError):
                logger.error("Operational evidence source returned invalid redacted fields.")
                quality.state = ObservationState.UNAVAILABLE
                rows = []
                warnings.append("invalid-request-evidence")
        return RequestEvidenceResponse(
            scope=self.data.config.scope,
            quality=quality,
            warnings=warnings,
            records=rows,
            truncated=truncated,
        )

    def alert_instances(self, query: DashboardQuery) -> AlertInstancesResponse:
        if query.filters():
            raise ValueError("alert instances support time-window filtering only")
        quality = self._quality(query, DataSource.ARM)
        warnings = []
        rows = []
        truncated = False
        if query.window.start < datetime.now(UTC) - timedelta(days=30):
            warnings.append("alert-history-supports-last-thirty-days")
        elif self.alerts is None:
            warnings.append("alert-reader-not-configured")
        else:
            try:
                result = self.alerts.read(
                    self.data.config.scope.subscription_id,
                    self.data.config.apim_resource_id,
                    query.window.start,
                    query.window.end,
                )
                truncated = result.partial
                for raw in result.rows:
                    essentials = raw.get("properties", {}).get("essentials", {})
                    if (
                        essentials.get("targetResource", "").casefold()
                        != self.data.config.apim_resource_id.casefold()
                    ):
                        raise ValueError("alert target did not match the authorized gateway")
                    alert_id = raw["id"]
                    prefix = (
                        f"/subscriptions/{self.data.config.scope.subscription_id}"
                        "/providers/Microsoft.AlertsManagement/alerts/"
                    )
                    if not isinstance(alert_id, str) or not alert_id.casefold().startswith(prefix.casefold()):
                        raise ValueError("alert identity is outside the configured subscription")
                    row = AlertInstance(
                        id=alert_id,
                        severity=essentials["severity"],
                        condition=essentials["monitorCondition"],
                        state=essentials["alertState"],
                        started_at=essentials["startDateTime"],
                        modified_at=essentials["lastModifiedDateTime"],
                        portal_url=portal_link(alert_id),
                    )
                    if row.condition != "Fired":
                        continue
                    rows.append(row)
                quality.state = ObservationState.PARTIAL if truncated else ObservationState.HEALTHY
                if truncated:
                    warnings.append("alert-page-limit-reached")
                if not rows:
                    warnings.append("no-fired-alerts-in-window-not-a-health-probe")
            except AzureError as exc:
                logger.warning("Alert instances failed (%s).", _failure(exc))
                warnings.append("alert-instances-" + _failure(exc))
            except (ValidationError, ValueError, KeyError, TypeError):
                logger.error("Alert source returned invalid scoped metadata.")
                rows = []
                warnings.append("invalid-alert-metadata")
        return AlertInstancesResponse(
            scope=self.data.config.scope,
            quality=quality,
            warnings=warnings,
            alerts=rows,
            truncated=truncated,
        )

    def pipeline_health(self, query: DashboardQuery) -> PipelineHealthResponse:
        if query.filters():
            raise ValueError("component metrics support time-window filtering only")
        quality = self._quality(query)
        warnings = ["component-metrics-are-not-an-ingestion-watermark"]
        components = []
        for kind in ("event_hub", "processor", "usage_store"):
            resource = self.data.config.pipeline_resources.get(kind)
            component = ComponentHealth(component=kind, state="unavailable")
            if resource is None:
                component.warnings.append("component-resource-not-configured")
            elif self.data.config.log_analytics_workspace_id is None:
                component.warnings.append("log-analytics-not-configured")
            else:
                component.portal_url = portal_link(resource)
                metric_names = ", ".join(json.dumps(value) for value in _COMPONENT_METRICS[kind])
                kql = (
                    f"AzureMetrics | where _ResourceId =~ {json.dumps(resource)} "
                    f"| where TimeGenerated >= datetime({query.window.start.isoformat()}) "
                    f"and TimeGenerated < datetime({query.window.end.isoformat()}) "
                    f"| where MetricName in~ ({metric_names}) and isnotnull(Total) "
                    "| summarize observed=max(TimeGenerated), samples=count(), total=sum(Total) "
                    "by MetricName "
                    "| take 101"
                )
                try:
                    result = self.data.logs.query_workspace(
                        self.data.config.log_analytics_workspace_id,
                        kql,
                        (query.window.start, query.window.end),
                    )
                    if result.partial or len(result.rows) > 100:
                        component.state = "partial"
                        component.warnings.append("component-metrics-partial")
                    elif not result.rows:
                        component.state = "unknown"
                        component.warnings.append("no-component-metrics-check-diagnostics-not-outage")
                    else:
                        latest = []
                        for item in result.rows:
                            name = item["MetricName"]
                            if not isinstance(name, str) or not name or len(name) > 100:
                                raise ValueError("invalid metric name")
                            observed = datetime.fromisoformat(str(item["observed"]).replace("Z", "+00:00"))
                            if (
                                observed.tzinfo is None
                                or not query.window.start <= observed < query.window.end
                                or observed > datetime.now(UTC) + timedelta(minutes=1)
                                or name.casefold()
                                not in {value.casefold() for value in _COMPONENT_METRICS[kind]}
                            ):
                                raise ValueError("metric observation must include a timezone")
                            latest.append(observed)
                            total = Decimal(str(item["total"]))
                            if not total.is_finite() or total < 0:
                                raise ValueError("invalid metric total")
                            component.metrics[name] = total
                        component.observed_at = max(latest)
                        component.state = (
                            "healthy"
                            if datetime.now(UTC) - component.observed_at <= timedelta(minutes=15)
                            else "stale"
                        )
                        if component.state == "stale":
                            component.warnings.append("component-metric-observation-delayed")
                        failed = any(
                            value > 0
                            and any(
                                word in name.casefold().replace(" ", "")
                                for word in (
                                    "runsfailed",
                                    "servererrors",
                                    "throttledrequests",
                                    "actionsfailed",
                                )
                            )
                            for name, value in component.metrics.items()
                        )
                        if failed:
                            component.state = "degraded"
                        component.warnings.append(
                            "namespace-or-app-level-metrics-not-gateway-end-to-end-health"
                        )
                except AzureError as exc:
                    logger.warning("Pipeline component telemetry failed (%s).", _failure(exc))
                    component.warnings.append("component-" + _failure(exc))
                except (ValueError, KeyError, TypeError, InvalidOperation):
                    logger.error("Pipeline component telemetry contained invalid fields.")
                    component.state = "unavailable"
                    component.metrics = {}
                    component.warnings.append("invalid-component-metrics")
            components.append(component)
        if self.analytics is not None:
            component = ComponentHealth(component="analytics_worker", state="unavailable")
            try:
                snapshot = self.analytics.latest()
                component.observed_at = snapshot.created_at
                component.state = (
                    "healthy" if datetime.now(UTC) - snapshot.created_at < timedelta(minutes=30) else "stale"
                )
                component.metrics = {
                    "retained_requests": Decimal(snapshot.event_count),
                    "quarantined_records": Decimal(snapshot.rejected_records),
                }
                component.warnings.append("worker-refresh-is-not-source-ingestion-completeness")
            except (AzureError, AnalyticsUnavailable):
                component.warnings.append("no-available-published-analytics-generation")
            components.append(component)
        states = {component.state for component in components}
        quality.state = (
            ObservationState.DEGRADED
            if "degraded" in states
            else ObservationState.STALE
            if "stale" in states
            else ObservationState.PARTIAL
            if "partial" in states
            else ObservationState.UNKNOWN
        )
        return PipelineHealthResponse(
            scope=self.data.config.scope, quality=quality, warnings=warnings, components=components
        )

    def attribution(self, query: DashboardQuery) -> OperationsAttribution:
        if self.analytics is None:
            raise AnalyticsUnavailable("Maintained usage attribution is not configured.")
        if query.uses_custom_dimensions() or any("custom_" in name for name in query.filters()):
            raise ValueError("operational attribution does not expose custom identifiers")
        snapshot = self.analytics.latest()
        groups = {}
        truncated = False
        for dimension in (
            UsageDimension.BACKEND,
            UsageDimension.MODEL,
            UsageDimension.DEPLOYMENT,
            UsageDimension.REGION,
            UsageDimension.APPLICATION,
        ):
            counts, limited = self.analytics.request_counts(
                snapshot, query.window, dimension, query.filters()
            )
            truncated |= limited
            groups[dimension] = [
                AttributionCount(value=label, requests=count)
                for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0] or ""))
            ]
        quality = self.analytics.quality(snapshot, query.window)
        warnings = [
            "usage-attribution-does-not-join-gateway-latency-or-response-codes",
            "operational-attribution-excludes-financial-and-custom-fields",
        ]
        if truncated:
            quality.state = ObservationState.PARTIAL
            warnings.append("attribution-group-limit-reached-narrow-filters")
        return OperationsAttribution(
            scope=self.data.config.scope,
            quality=quality,
            warnings=warnings,
            read_model_version=snapshot.generation,
            groups=groups,
            truncated=truncated,
        )
