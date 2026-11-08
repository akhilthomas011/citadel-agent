# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Bounded live dashboard queries using the shared normalization and pricing semantics."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any
from urllib.parse import urlsplit

import httpx
from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError
from pydantic import ValidationError

from citadel_agent.analytics.pricing import ModelPricingEntry, PricingStatus, estimate_event_cost
from citadel_agent.analytics.quality import assess_usage_quality
from citadel_agent.analytics.rollups import aggregate_finops_events
from citadel_agent.analytics.usage import (
    NormalizedUsageEvent,
    UsageNormalizationError,
    UsageSourceKey,
    normalize_usage_document,
)
from citadel_agent.azure_clients.apim import ApimClient
from citadel_agent.azure_clients.cosmos import LLM_USAGE_CONTAINER, MODEL_PRICING_CONTAINER, CosmosUsageReader
from citadel_agent.azure_clients.monitor import LogQuery
from citadel_agent.gateway.client import GatewayClient, GatewayRequestError
from citadel_agent.portal.contracts import (
    CostBasis,
    CountValue,
    DataQuality,
    DataSource,
    FinOpsAggregate,
    HubScope,
    InventorySnapshot,
    MonetaryAmount,
    ObservationState,
    OperationalMetric,
    TokenUsage,
)
from citadel_agent.portal.product_contracts import (
    DashboardQuery,
    FinOpsResponse,
    OperationsBucket,
    OperationsQuery,
    OperationsResponse,
    PortalHubConfig,
)

logger = logging.getLogger(__name__)


class PortalDataService:
    def __init__(
        self,
        config: PortalHubConfig,
        credential: TokenCredential,
        *,
        usage_reader: CosmosUsageReader | None = None,
        logs: LogQuery | None = None,
        canonical_counts: Callable[[], dict[str, int]] | None = None,
    ) -> None:
        self.config = config
        self.credential = credential
        self._owns_usage = usage_reader is None
        self._owns_logs = logs is None
        self.usage = usage_reader or CosmosUsageReader(credential, config.usage_endpoint)
        self.logs = logs or LogQuery(credential)
        self.canonical_counts = canonical_counts
        self._inventory_lock = threading.Lock()
        self._inventory_cache: InventorySnapshot | None = None
        self._inventory_expires = 0.0

    def close(self) -> None:
        if self._owns_usage:
            self.usage.close()
        if self._owns_logs:
            self.logs.close()

    def inventory(self) -> InventorySnapshot:
        with self._inventory_lock:
            if self._inventory_cache is None or time.monotonic() >= self._inventory_expires:
                self._inventory_cache = self._read_inventory()
                self._inventory_expires = time.monotonic() + 60
            return self._inventory_cache.model_copy(deep=True)

    def _read_inventory(self) -> InventorySnapshot:
        now = datetime.now(UTC)
        warnings: list[str] = []
        counts: dict[str, CountValue] = {}

        def count(name: str, value: int | None, source: DataSource) -> None:
            counts[name] = CountValue(
                value=value,
                state=ObservationState.HEALTHY if value is not None else ObservationState.UNAVAILABLE,
                source=source,
                observed_at=now,
            )

        try:
            apim = ApimClient(
                self.credential,
                self.config.scope.subscription_id,
                self.config.apim_resource_group,
                self.config.apim_name,
            )
            count("backends", len(apim.list_backends()), DataSource.ARM)
        except AzureError:
            logger.warning("Portal backend inventory is unavailable.")
            count("backends", None, DataSource.ARM)
            warnings.append("backend-inventory-unavailable")

        gateway = GatewayClient(self.config.gateway_url)
        try:
            contract = gateway.backend_contract()
            deployments = {
                (backend.get("backendId"), model.get("name") if isinstance(model, dict) else model)
                for backend in contract.backends()
                for model in backend.get("supportedModels", [])
            }
            count("model_deployments", len(deployments), DataSource.APIM)
        except (GatewayRequestError, httpx.HTTPError, ValueError):
            logger.warning("Portal live model inventory is unavailable.")
            count("model_deployments", None, DataSource.APIM)
            warnings.append("model-inventory-unavailable")
        finally:
            gateway.close()

        canonical: dict[str, int] = {}
        if self.canonical_counts is not None:
            canonical = self.canonical_counts()
        for name in ("access_contracts", "mcp_tools", "published_agents"):
            count(name, canonical.get(name), DataSource.CANONICAL_GIT)
        if not canonical:
            warnings.append("canonical-inventory-unavailable")
        count("active_alerts", None, DataSource.ARM)
        warnings.append("active-alert-instances-not-connected")
        return InventorySnapshot(
            scope=self.config.scope,
            state=ObservationState.PARTIAL if warnings else ObservationState.HEALTHY,
            observed_at=now,
            warnings=warnings,
            **counts,
        )

    def finops(self, query: DashboardQuery) -> FinOpsResponse:
        documents, truncated = self._scan(
            LLM_USAGE_CONTAINER,
            "SELECT * FROM c WHERE c.gatewayName = @gateway ORDER BY c._ts DESC",
            {"gateway": self.config.apim_name},
            self.config.max_usage_records,
        )
        events: list[NormalizedUsageEvent] = []
        rejected = 0
        for document in documents:
            try:
                event = self.normalize(document)
            except (UsageNormalizationError, ValidationError, KeyError, ValueError):
                rejected += 1
                continue
            if query.window.start <= event.event_time < query.window.end:
                events.append(event)

        options = {
            name: sorted({value for event in events if (value := getattr(event.dimensions, name))})
            for name in ("model", "product", "application")
        }
        events = [
            event
            for event in events
            if all(getattr(event.dimensions, name) == value for name, value in query.filters().items())
        ]
        now = datetime.now(UTC)
        quality = assess_usage_quality(
            window=query.window,
            observed_at=now,
            watermark=None,
            max_watermark_age=timedelta(minutes=30),
            events=events,
            rejected_records=rejected,
        )
        warnings = [
            "costs-are-usage-estimates-not-invoices",
            "bounded-live-preview-use-report-run-for-complete-scan",
        ]
        if truncated:
            warnings.append("usage-scan-limit-reached")
            quality.state = ObservationState.PARTIAL
        if rejected:
            warnings.append("malformed-usage-records-excluded")
        prices, pricing_warnings = self.pricing_snapshot()
        warnings.extend(pricing_warnings)

        costs = {event.event_id: estimate_event_cost(event, prices) for event in events}
        aggregates = aggregate_finops_events(
            events, costs, window=query.window, quality=quality, group_by=(query.group_by,)
        )
        return summarize_finops(
            self.config.scope,
            query,
            quality=quality,
            aggregates=aggregates,
            requests=len(events),
            unpriced_requests=sum(cost.status is PricingStatus.UNPRICED for cost in costs.values()),
            rejected_records=rejected,
            scanned_records=len(documents),
            truncated=truncated,
            warnings=warnings,
            options=options,
            usage_weight=sum(event.usage.prompt + event.usage.completion for event in events),
            priced_weight=sum(
                event.usage.prompt + event.usage.completion
                for event in events
                if costs[event.event_id].status is PricingStatus.PRICED
            ),
        )

    def normalize(self, document: dict[str, Any]) -> NormalizedUsageEvent:
        return normalize_usage_document(
            document, source_key=self.source_key(document), scope=self.config.scope
        )

    def source_key(self, document: dict[str, Any]) -> UsageSourceKey:
        if document.get("gatewayName") != self.config.apim_name:
            raise UsageNormalizationError("gateway-scope-mismatch", "gatewayName")
        if self.config.usage_partition_field not in document:
            raise UsageNormalizationError("missing-partition-key", self.config.usage_partition_field)
        return UsageSourceKey.create(
            account=urlsplit(self.config.usage_endpoint).hostname or self.config.usage_endpoint,
            database=self.config.usage_database,
            container=LLM_USAGE_CONTAINER,
            partition_key=document.get(self.config.usage_partition_field),
            document_id=document["id"],
        )

    def pricing_snapshot(self) -> tuple[list[ModelPricingEntry], list[str]]:
        prices: list[ModelPricingEntry] = []
        warnings: list[str] = []
        try:
            raw, truncated = self._scan(MODEL_PRICING_CONTAINER, "SELECT * FROM c", {}, 2000)
            if truncated:
                return [], ["pricing-scan-limit-reached"]
            for item in raw:
                try:
                    prices.append(ModelPricingEntry.model_validate(_pricing_fields(item)))
                except ValidationError:
                    warnings.append("invalid-pricing-records-excluded")
        except AzureError:
            logger.warning("Portal pricing source is unavailable.")
            warnings.append("pricing-source-unavailable")
        return prices, list(dict.fromkeys(warnings))

    def operations(self, query: DashboardQuery) -> OperationsResponse:
        if query.filters():
            raise ValueError("operations supports time-window filtering only")
        now = datetime.now(UTC)
        quality = DataQuality(
            source=DataSource.LOG_ANALYTICS,
            observed_at=now,
            event_window=query.window,
            state=ObservationState.UNAVAILABLE,
        )
        warnings: list[str] = []
        rows: list[dict[str, Any]] = []
        if self.config.log_analytics_workspace_id is None:
            warnings.append("log-analytics-not-configured")
        else:
            resource = json.dumps(self.config.apim_resource_id)
            start = query.window.start.isoformat()
            end = query.window.end.isoformat()
            bucket = "1h" if query.window.end - query.window.start <= timedelta(days=7) else "1d"
            summary = (
                "requests=count(), failures=countif(ResponseCode >= 500), "
                "throttled=countif(ResponseCode == 429), authFailures=countif(ResponseCode in (401,403)), "
                "p95Ms=percentile(TotalTime,95)"
            )
            kql = (
                f"let scoped = ApiManagementGatewayLogs | where _ResourceId =~ {resource} "
                f"| where TimeGenerated >= datetime({start}) and TimeGenerated < datetime({end}) "
                + gateway_filter_kql(query.gateway_filters if isinstance(query, OperationsQuery) else {})
                + ";\n"
                f"union (scoped | summarize {summary} | extend kind='total'), "
                f"(scoped | summarize {summary} by timestamp=bin(TimeGenerated,{bucket}) "
                "| extend kind='bucket')"
            )
            try:
                result = self.logs.query_workspace(
                    self.config.log_analytics_workspace_id, kql, (query.window.start, query.window.end)
                )
                rows = result.rows
                quality.state = ObservationState.PARTIAL if result.partial else ObservationState.HEALTHY
                if result.partial:
                    warnings.append("log-analytics-partial-result")
            except AzureError:
                logger.warning("Portal operational telemetry is unavailable.")
                warnings.append("log-analytics-query-failed")

        total = next((row for row in rows if row.get("kind") == "total"), {})
        metrics: list[OperationalMetric] = []
        for name, key, unit in (
            ("requests", "requests", "requests"),
            ("server-errors", "failures", "requests"),
            ("throttled", "throttled", "requests"),
            ("auth-failures", "authFailures", "requests"),
            ("latency-p95", "p95Ms", "ms"),
        ):
            value = total.get(key)
            metric_quality = quality.model_copy(deep=True)
            if value is None:
                metric_quality.state = ObservationState.UNAVAILABLE
            metrics.append(
                OperationalMetric(
                    name=name,
                    value=Decimal(str(value)) if value is not None else None,
                    unit=unit,
                    state=metric_quality.state,
                    quality=metric_quality,
                )
            )
        requests = total.get("requests")
        if requests == 0 and quality.state is ObservationState.HEALTHY:
            quality.state = ObservationState.UNKNOWN
            warnings.append("no-observed-traffic-is-not-verified-health")
            for metric in metrics:
                if metric.value is not None:
                    metric.state = ObservationState.PARTIAL
                    metric.quality.state = ObservationState.PARTIAL
        failures = total.get("failures")
        rate = (
            Decimal(requests - failures) * 100 / Decimal(requests)
            if requests and failures is not None
            else None
        )
        metrics.append(
            OperationalMetric(
                name="non-server-error-rate",
                value=rate,
                unit="percent",
                state=quality.state if rate is not None else ObservationState.UNKNOWN,
                quality=quality,
            )
        )
        timeline = [
            OperationsBucket(
                timestamp=row["timestamp"],
                requests=row["requests"],
                failures=row["failures"],
                throttled=row["throttled"],
                p95_ms=row.get("p95Ms"),
            )
            for row in rows
            if row.get("kind") == "bucket"
        ]
        if not timeline and quality.state is ObservationState.HEALTHY:
            warnings.append("no-gateway-request-logs-in-window")
        warnings.append("request-telemetry-is-not-an-availability-probe")
        return OperationsResponse(
            scope=self.config.scope,
            quality=quality,
            warnings=warnings,
            window=query.window,
            metrics=metrics,
            timeline=sorted(timeline, key=lambda item: item.timestamp),
        )

    def _scan(
        self, container: str, sql: str, parameters: dict[str, object], limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        documents: list[dict[str, Any]] = []
        ranges = self.usage.read_feed_ranges(self.config.usage_database, container)
        budget = limit // 500 + len(ranges) + 2
        for index, feed_range in enumerate(ranges):
            token: str | None = None
            while budget > 0:
                budget -= 1
                page = self.usage.query_page(
                    self.config.usage_database,
                    container,
                    sql,
                    parameters,
                    page_size=min(500, limit - len(documents)),
                    continuation_token=token,
                    feed_range=feed_range,
                )
                documents.extend(page.items)
                previous, token = token, page.continuation_token
                if token is not None and token == previous:
                    raise RuntimeError("Cosmos pagination did not advance.")
                if len(documents) >= limit:
                    return documents[:limit], token is not None or index < len(ranges) - 1
                if token is None:
                    break
            else:
                return documents, True
        return documents, False


def _pricing_fields(raw: dict[str, Any]) -> dict[str, Any]:
    accepted = set(ModelPricingEntry.model_fields)
    accepted.update(field.alias for field in ModelPricingEntry.model_fields.values() if field.alias)
    return {key: value for key, value in raw.items() if key in accepted}


GATEWAY_COLUMNS = {
    "backend": "BackendId",
    "product_id": "ProductId",
    "subscription_id": "ApimSubscriptionId",
    "region": "Region",
    "api_id": "ApiId",
    "operation_name": "OperationName",
}


def gateway_filter_kql(filters: Mapping[str, str | None]) -> str:
    parts = []
    for name, value in filters.items():
        if name not in GATEWAY_COLUMNS:
            raise ValueError("unsupported gateway log filter")
        column = GATEWAY_COLUMNS[name]
        parts.append(
            f"| where isempty({column})" if value is None else f"| where {column} == {json.dumps(value)}"
        )
    return " ".join(parts)


def summarize_finops(
    scope: HubScope,
    query: DashboardQuery,
    *,
    quality: DataQuality,
    aggregates: list[FinOpsAggregate],
    requests: int,
    unpriced_requests: int,
    scanned_records: int,
    rejected_records: int,
    truncated: bool,
    warnings: list[str],
    options: dict[str, list[str]],
    usage_weight: int,
    priced_weight: int,
) -> FinOpsResponse:
    """Share presentation/accounting semantics between previews and checkpointed reports."""
    quality = quality.model_copy(deep=True)
    quality.warning_codes = list(
        dict.fromkeys(
            [
                *quality.warning_codes,
                *(code for row in aggregates for code in row.quality.warning_codes),
            ]
        )
    )
    token_values = {
        name: sum(getattr(row.usage, name) or 0 for row in aggregates) for name in TokenUsage.model_fields
    }
    if not aggregates or any(row.usage.total is None for row in aggregates):
        token_values["total"] = None
    amounts: dict[str, Decimal] = defaultdict(Decimal)
    with localcontext() as context:
        context.prec = 100
        for row in aggregates:
            for cost in row.costs:
                amounts[cost.currency] += cost.amount
        coverage = Decimal(priced_weight) * 100 / usage_weight if usage_weight else Decimal(0)
    warnings = list(
        dict.fromkeys(
            [
                *warnings,
                *(code for row in aggregates for code in row.pricing_coverage.warning_codes),
            ]
        )
    )
    return FinOpsResponse(
        scope=scope,
        quality=quality,
        warnings=warnings,
        window=query.window,
        requests=requests,
        usage=TokenUsage(**token_values),
        costs=[
            MonetaryAmount(
                amount=amount,
                currency=currency,
                basis=CostBasis.USAGE_ESTIMATE,
                source=DataSource.COSMOS_USAGE,
            )
            for currency, amount in sorted(amounts.items())
        ],
        priced_usage_percent=coverage,
        unpriced_requests=unpriced_requests,
        rejected_records=rejected_records,
        scanned_records=scanned_records,
        truncated=truncated,
        aggregates=aggregates,
        models=options["model"],
        products=options["product"],
        applications=options["application"],
    )
