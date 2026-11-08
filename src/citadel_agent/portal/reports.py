# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Durable, source-paginated reports with atomic aggregate/cursor checkpoints.

An exhausted source scan is not an ingestion watermark or transactional Cosmos
snapshot. The response keeps that distinction explicit; fixed-period allocation
still requires the later maintained read-model/completeness milestone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from azure.core.exceptions import AzureError
from pydantic import Field, ValidationError

from citadel_agent.analytics.pricing import ModelPricingEntry, PricingStatus, estimate_event_cost
from citadel_agent.analytics.read_model import AnalyticsReader, dimensions_match
from citadel_agent.analytics.rollups import FinOpsCheckpoint, IncrementalFinOps, UsageDimension
from citadel_agent.analytics.snapshots import AnalyticsUnavailable
from citadel_agent.analytics.usage import NormalizedUsageEvent, UsageNormalizationError
from citadel_agent.analytics.views import (
    CUSTOM_DIMENSIONS,
    STANDARD_DIMENSIONS,
    AnalysisCheckpoint,
    AnalyticalAccumulator,
)
from citadel_agent.azure_clients.cosmos import LLM_USAGE_CONTAINER
from citadel_agent.contracts.common import CitadelModel
from citadel_agent.portal.contracts import DataQuality, DataSource, ObservationState
from citadel_agent.portal.data import PortalDataService, summarize_finops
from citadel_agent.portal.product_contracts import (
    DashboardQuery,
    FinOpsReportResponse,
    FinOpsResponse,
    ReportStartRequest,
)
from citadel_agent.portal.sessions import PortalStateConflict
from citadel_agent.portal.state import ProductRecordTooLarge, ProductStore, VersionedDocument

logger = logging.getLogger(__name__)
PAGE_SIZE = 500
PAGES_PER_ADVANCE = 4
LEASE_SECONDS = 120
MAX_BUCKETS = 1000
MAX_OPTIONS = 2000
MAX_ANALYSIS_BUCKETS = 1500
_KIND = "finops-report"
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class ReportUnavailable(RuntimeError):
    """Source processing failed transiently; the committed report can be resumed."""


class _ReportData(CitadelModel):
    version: Literal[1] = 1
    query: DashboardQuery
    include_analysis: bool = False
    custom_dimensions: tuple[UsageDimension, ...] = ()
    analysis_checkpoint: AnalysisCheckpoint | None = None
    input_kind: Literal["source-scan", "read-model"] = "source-scan"
    read_model_version: str | None = None
    source_fingerprint: str
    status: Literal["pending", "running", "succeeded", "failed", "cancelled"] = "pending"
    continuation: str | None = None
    feed_ranges: list[dict[str, Any]]
    range_index: int = Field(default=0, ge=0)
    source_cutoff: int
    pricing_snapshot_at: datetime
    prices: list[ModelPricingEntry]
    warnings: list[str] = Field(default_factory=list)
    quality_warnings: list[str] = Field(default_factory=list)
    checkpoint: FinOpsCheckpoint | None
    options: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "model": [],
            "product": [],
            "application": [],
        }
    )
    scanned_records: int = Field(default=0, ge=0)
    matched_requests: int = Field(default=0, ge=0)
    unpriced_requests: int = Field(default=0, ge=0)
    rejected_records: int = Field(default=0, ge=0)
    lease_until: datetime | None = None
    error_code: str | None = None
    result: FinOpsResponse | None = None


class FinOpsReportService:
    def __init__(
        self,
        store: ProductStore,
        data: PortalDataService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        analytics: AnalyticsReader | None = None,
    ) -> None:
        if store.scope != data.config.scope:
            raise ValueError("report state and source must use the same authorized hub scope")
        self.store = store
        self.data = data
        self.clock = clock
        self.analytics = analytics
        config = data.config
        source = [
            config.scope.model_dump(mode="json"),
            config.usage_endpoint.casefold().rstrip("/"),
            config.usage_database,
            config.apim_name,
            config.usage_partition_field,
            PAGE_SIZE,
        ]
        self.fingerprint = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()

    def start(
        self, request: ReportStartRequest, subject_id: str, *, custom_access: bool = False
    ) -> FinOpsReportResponse:
        configured = tuple(UsageDimension(name) for name in self.data.config.custom_dimension_labels)
        if request.include_custom_dimensions and not configured:
            raise ValueError("Custom attribution must be configured by the platform administrator.")
        requested = tuple(
            value
            for value in CUSTOM_DIMENSIONS
            if (
                (request.include_custom_dimensions and value in configured)
                or request.query.group_by == value
                or value in request.query.dimension_filters
            )
        )
        if requested and not custom_access:
            raise PermissionError("Custom attribution requires a separate sensitive-data grant.")
        if any(value not in configured for value in requested):
            raise ValueError("Custom attribution must be enabled and labelled by the platform administrator.")
        key = hashlib.sha256(f"{subject_id.lower()}:{request.request_id}".encode()).hexdigest()
        try:
            existing = self.store.get(_KIND, key, subject_id=subject_id)
        except KeyError:
            existing = None
        if existing is not None:
            data = self._data(existing)
            if (
                data.query != request.query
                or data.include_analysis != request.include_analysis
                or (request.input_kind is not None and request.input_kind != data.input_kind)
                or data.custom_dimensions != requested
                or (
                    request.read_model_version is not None
                    and request.read_model_version != data.read_model_version
                )
            ):
                raise PortalStateConflict("This report request ID was used for a different query.")
            return self._view(existing)
        now = self.clock()
        kind = request.input_kind or ("read-model" if self.analytics is not None else "source-scan")
        if request.read_model_version is not None and kind != "read-model":
            raise ValueError("Pinned analytical generations require maintained analytics.")
        if kind == "read-model":
            if self.analytics is None:
                raise PortalStateConflict("Maintained analytics are not configured.")
            info = (
                self.analytics.snapshot(request.read_model_version)
                if request.read_model_version is not None
                else self.analytics.latest()
            )
            self.analytics.validate_window(info, request.query.window)
            if info.pricing_snapshot_at is None:
                raise AnalyticsUnavailable("The maintained snapshot has no captured pricing metadata.")
            record = _ReportData(
                query=request.query,
                input_kind=kind,
                read_model_version=info.generation,
                source_fingerprint=self.analytics.fingerprint,
                source_cutoff=info.source_cutoff,
                pricing_snapshot_at=info.pricing_snapshot_at,
                prices=[],
                feed_ranges=[],
                warnings=info.warnings,
                rejected_records=info.rejected_records,
                checkpoint=IncrementalFinOps(
                    self.store.scope, request.query.window, (request.query.group_by,)
                ).checkpoint(),
            )
            return self._create(key, record, subject_id, request, requested)
        ranges = self.data.usage.read_feed_ranges(self.data.config.usage_database, LLM_USAGE_CONTAINER)
        if len({json.dumps(value, sort_keys=True) for value in ranges}) != len(ranges):
            raise ValueError("the source returned duplicate routing ranges")
        prices, warnings = self.data.pricing_snapshot()
        record = _ReportData(
            query=request.query,
            source_fingerprint=self.fingerprint,
            source_cutoff=int(now.timestamp()),
            pricing_snapshot_at=self.clock(),
            prices=prices,
            warnings=warnings,
            feed_ranges=list(ranges),
            checkpoint=IncrementalFinOps(
                self.store.scope, request.query.window, (request.query.group_by,)
            ).checkpoint(),
        )
        return self._create(key, record, subject_id, request, requested)

    def _create(
        self,
        key: str,
        record: _ReportData,
        subject_id: str,
        request: ReportStartRequest,
        custom_dimensions: tuple[UsageDimension, ...],
    ) -> FinOpsReportResponse:
        record.include_analysis = request.include_analysis
        record.custom_dimensions = custom_dimensions
        if record.include_analysis:
            record.analysis_checkpoint = AnalyticalAccumulator(
                self.store.scope, record.query.window, custom_dimensions
            ).checkpoint()
            record.options = {name: [] for name in (*STANDARD_DIMENSIONS, *custom_dimensions)}
        try:
            created = self.store.create(_KIND, key, subject_id, record.model_dump(by_alias=True, mode="json"))
        except ProductRecordTooLarge:
            raise
        except PortalStateConflict:
            created = self.store.get(_KIND, key, subject_id=subject_id)
            existing = self._data(created)
            if (
                existing.query != record.query
                or existing.input_kind != record.input_kind
                or existing.include_analysis != record.include_analysis
                or existing.custom_dimensions != record.custom_dimensions
                or (
                    request.read_model_version is not None
                    and existing.read_model_version != record.read_model_version
                )
            ):
                raise PortalStateConflict("This report request ID was used for a different query.") from None
        return self._view(created)

    def get(self, report_id: str, subject_id: str) -> FinOpsReportResponse:
        return self._view(self.store.get(_KIND, report_id, subject_id=subject_id))

    def requires_custom(self, report_id: str, subject_id: str) -> bool:
        data = self._data(self.store.get(_KIND, report_id, subject_id=subject_id))
        return bool(data.custom_dimensions) or data.query.uses_custom_dimensions()

    def recent(self, subject_id: str, *, custom_access: bool = False) -> list[FinOpsReportResponse]:
        return [
            self._view(item, include_result=False)
            for item in self.store.list(_KIND, subject_id=subject_id, limit=30)
            if custom_access
            or not (self._data(item).custom_dimensions or self._data(item).query.uses_custom_dimensions())
        ]

    def cancel(self, report_id: str, subject_id: str) -> FinOpsReportResponse:
        current = self.store.get(_KIND, report_id, subject_id=subject_id)
        state = self._data(current)
        if state.status in _TERMINAL:
            return self._view(current)
        state.status = "cancelled"
        state.checkpoint = None
        state.analysis_checkpoint = None
        state.prices = []
        state.lease_until = None
        return self._view(self._save(current, state))

    def completed(self, report_id: str, subject_id: str) -> tuple[DashboardQuery, FinOpsResponse]:
        current = self.store.get(_KIND, report_id, subject_id=subject_id)
        state = self._data(current)
        if state.status != "succeeded" or state.result is None or not state.result.source_scan_complete:
            raise PortalStateConflict("Only a completed source-scan report can be exported.")
        return state.query, state.result

    def advance(self, report_id: str, subject_id: str) -> FinOpsReportResponse:
        current = self.store.get(_KIND, report_id, subject_id=subject_id)
        state = self._data(current)
        if state.status in _TERMINAL:
            return self._view(current)
        expected = (
            self.analytics.fingerprint
            if state.input_kind == "read-model" and self.analytics is not None
            else self.fingerprint
        )
        if state.source_fingerprint != expected:
            raise PortalStateConflict("The report source configuration changed; create a new report.")
        now = self.clock()
        if state.lease_until is not None and state.lease_until > now:
            return self._view(current)
        state.status = "running"
        state.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        state.error_code = None
        try:
            current = self._save(current, state)
        except ProductRecordTooLarge:
            return self._fail(current, "report-result-too-large")
        except PortalStateConflict:
            return self.get(report_id, subject_id)
        began = time.monotonic()
        try:
            if state.checkpoint is None:
                raise ValueError("a resumable report must contain its aggregation checkpoint")
            accumulator = IncrementalFinOps.restore(state.checkpoint)
            analysis = None
            if state.include_analysis:
                if state.analysis_checkpoint is None:
                    raise ValueError("the analytical report has no committed projection checkpoint")
                analysis = AnalyticalAccumulator(
                    self.store.scope, state.query.window, state.custom_dimensions
                )
                analysis.restore(state.analysis_checkpoint)
            if (
                accumulator.scope != self.store.scope
                or accumulator.window != state.query.window
                or accumulator.group_by != (state.query.group_by,)
            ):
                raise ValueError("report checkpoint scope, window or grouping does not match its query")
            for _ in range(PAGES_PER_ADVANCE):
                done = (
                    self._maintained_page(state, accumulator, analysis)
                    if state.input_kind == "read-model"
                    else self._source_page(state, accumulator, analysis)
                )
                if (
                    accumulator.bucket_count > MAX_BUCKETS
                    or sum(map(len, state.options.values())) > MAX_OPTIONS
                    or (analysis is not None and analysis.bucket_count > MAX_ANALYSIS_BUCKETS)
                ):
                    return self._fail(current, "report-cardinality-limit")
                state.quality_warnings = sorted(set(state.quality_warnings))
                if done:
                    state.result = self._finish(report_id, state, accumulator, analysis)
                    state.status = "succeeded"
                    state.checkpoint = None
                    state.analysis_checkpoint = None
                    state.prices = []
                    state.lease_until = None
                else:
                    state.checkpoint = accumulator.checkpoint()
                    state.analysis_checkpoint = analysis.checkpoint() if analysis is not None else None
                    state.lease_until = self.clock() + timedelta(seconds=LEASE_SECONDS)
                current = self._save(current, state)
                if state.status == "succeeded":
                    return self._view(current)
                if time.monotonic() - began >= 3:
                    break
            state.status = "pending"
            state.lease_until = None
            return self._view(self._save(current, state))
        except AzureError as exc:
            logger.warning("FinOps report source read failed; committed progress can be resumed.")
            state = self._data(current)
            state.status = "pending"
            state.error_code = "report-source-unavailable"
            state.lease_until = None
            try:
                self._save(current, state)
            except PortalStateConflict:
                return self.get(report_id, subject_id)
            raise ReportUnavailable(
                "Report source is unavailable. Resume this report to retry its checkpoint."
            ) from exc
        except ProductRecordTooLarge:
            return self._fail(current, "report-result-too-large")
        except AnalyticsUnavailable:
            return self._fail(current, "read-model-snapshot-unavailable")
        except PortalStateConflict:
            latest = self.store.get(_KIND, report_id, subject_id=subject_id)
            if latest.etag != current.etag:
                return self._view(latest)
            return self._fail(current, "report-checkpoint-conflict")
        except (ValueError, RuntimeError) as exc:
            logger.error("FinOps report processing failed (%s).", type(exc).__name__)
            return self._fail(current, "report-processing-failed")

    def _source_page(
        self,
        state: _ReportData,
        accumulator: IncrementalFinOps,
        analysis: AnalyticalAccumulator | None,
    ) -> bool:
        if state.range_index >= len(state.feed_ranges):
            raise ValueError("source routing-range checkpoint is invalid")
        page = self.data.usage.query_page(
            self.data.config.usage_database,
            LLM_USAGE_CONTAINER,
            "SELECT * FROM c WHERE c.gatewayName = @gateway AND c._ts <= @cutoff",
            {"gateway": self.data.config.apim_name, "cutoff": state.source_cutoff},
            page_size=PAGE_SIZE,
            continuation_token=state.continuation,
            feed_range=state.feed_ranges[state.range_index],
        )
        if len(page.items) > PAGE_SIZE or (
            page.continuation_token is not None and page.continuation_token == state.continuation
        ):
            raise ValueError("source pagination did not make valid bounded progress")
        events: list[NormalizedUsageEvent] = []
        for document in page.items:
            state.scanned_records += 1
            try:
                event = self.data.normalize(document)
            except (UsageNormalizationError, KeyError, ValueError):
                state.rejected_records += 1
                continue
            if not state.query.window.start <= event.event_time < state.query.window.end:
                continue
            for name in state.options:
                value = getattr(event.dimensions, name)
                if value is not None and value not in state.options[name]:
                    state.options[name].append(value)
            if dimensions_match(
                event.dimensions.model_dump(),
                state.query.filters(),
            ):
                events.append(event)
                state.quality_warnings.extend(event.warning_codes)
        costs = {event.event_id: estimate_event_cost(event, state.prices) for event in events}
        accumulator.add(events, costs)
        if analysis is not None:
            analysis.add(events, costs)
        state.matched_requests += len(events)
        state.unpriced_requests += sum(cost.status is PricingStatus.UNPRICED for cost in costs.values())
        state.continuation = page.continuation_token
        if state.continuation is None:
            state.range_index += 1
        return state.range_index == len(state.feed_ranges)

    def _maintained_page(
        self,
        state: _ReportData,
        accumulator: IncrementalFinOps,
        analysis: AnalyticalAccumulator | None,
    ) -> bool:
        if self.analytics is None or state.read_model_version is None:
            raise AnalyticsUnavailable("The report's maintained input is not configured.")
        info = self.analytics.snapshot(state.read_model_version)
        filters = state.query.filters()

        def options(dimensions: dict[str, Any]) -> None:
            for name in state.options:
                value = dimensions.get(name)
                if value is not None and value not in state.options[name]:
                    state.options[name].append(value)

        page = self.analytics.page(info, state.query.window, state.continuation)
        for record in page.events:
            event, cost = record.observation, record.cost
            if event is None or cost is None:
                raise AnalyticsUnavailable("The maintained event is unfinished.")
            dimensions = event.dimensions.model_dump()
            options(dimensions)
            if dimensions_match(dimensions, filters):
                accumulator.add([event], {event.event_id: cost})
                if analysis is not None:
                    analysis.add([event], {event.event_id: cost})
                state.matched_requests += 1
                state.unpriced_requests += int(cost.status is PricingStatus.UNPRICED)
                state.quality_warnings.extend(event.warning_codes)
        for rollup in page.rollups:
            bucket = rollup.checkpoint.buckets[0]
            dimensions = dict(zip(rollup.checkpoint.group_by, bucket.dimensions, strict=True))
            options(dimensions)
            if dimensions_match(dimensions, filters):
                accumulator.merge(rollup.checkpoint)
                if analysis is not None:
                    analysis.merge(rollup.checkpoint)
                state.matched_requests += bucket.requests
                state.unpriced_requests += bucket.unpriced_requests
        state.scanned_records += page.records
        state.continuation = page.continuation
        return state.continuation is None

    def _finish(
        self,
        report_id: str,
        state: _ReportData,
        accumulator: IncrementalFinOps,
        analysis: AnalyticalAccumulator | None,
    ) -> FinOpsResponse:
        quality = DataQuality(
            source=DataSource.COSMOS_USAGE,
            observed_at=self.clock(),
            event_window=state.query.window,
            state=ObservationState.PARTIAL if state.rejected_records else ObservationState.UNKNOWN,
            warning_codes=[
                "source-watermark-unavailable",
                "source-read-is-not-transactional-snapshot",
                *state.quality_warnings,
                *(["usage-records-quarantined"] if state.rejected_records else []),
            ],
        )
        if state.input_kind == "read-model":
            if self.analytics is None or state.read_model_version is None:
                raise AnalyticsUnavailable("The maintained report input is unavailable.")
            quality = self.analytics.quality(
                self.analytics.snapshot(state.read_model_version), state.query.window
            )
            quality.warning_codes = list(dict.fromkeys([*quality.warning_codes, *state.quality_warnings]))
        result = summarize_finops(
            self.store.scope,
            state.query,
            quality=quality,
            aggregates=accumulator.results(quality),
            requests=state.matched_requests,
            unpriced_requests=state.unpriced_requests,
            scanned_records=state.scanned_records,
            rejected_records=state.rejected_records,
            truncated=False,
            warnings=["costs-are-usage-estimates-not-invoices", *state.warnings],
            options={name: sorted(values) for name, values in state.options.items()},
            usage_weight=accumulator.usage_weight,
            priced_weight=accumulator.priced_weight,
        )
        result.report_id = report_id
        result.source_scan_complete = True
        result.input_kind = state.input_kind
        result.read_model_version = state.read_model_version
        result.filter_options = state.options
        result.analysis = analysis.result(quality) if analysis is not None else None
        return result

    def _fail(self, current: VersionedDocument, code: str) -> FinOpsReportResponse:
        logger.error("FinOps report stopped (%s). Partial totals will not be published.", code)
        state = self._data(current)
        state.status = "failed"
        state.error_code = code
        state.checkpoint = None
        state.analysis_checkpoint = None
        state.prices = []
        state.lease_until = None
        state.result = None
        try:
            return self._view(self._save(current, state))
        except PortalStateConflict:
            return self.get(current.record.key, current.record.subject_id)

    def _save(self, current: VersionedDocument, state: _ReportData) -> VersionedDocument:
        return self.store.replace(current, state.model_dump(by_alias=True, mode="json"))

    @staticmethod
    def _data(current: VersionedDocument) -> _ReportData:
        try:
            return _ReportData.model_validate(current.record.data)
        except ValidationError as exc:
            raise ReportUnavailable(
                "Stored report state is invalid; contact the platform administrator."
            ) from exc

    def _view(self, current: VersionedDocument, *, include_result: bool = True) -> FinOpsReportResponse:
        state = self._data(current)
        return FinOpsReportResponse(
            id=current.record.key,
            scope=current.record.scope,
            query=state.query,
            include_analysis=state.include_analysis,
            custom_dimensions=list(state.custom_dimensions),
            status=state.status,
            scanned_records=state.scanned_records,
            matched_requests=state.matched_requests,
            rejected_records=state.rejected_records,
            updated_at=current.record.updated_at,
            expires_at=current.record.expires_at,
            pricing_snapshot_at=state.pricing_snapshot_at,
            input_kind=state.input_kind,
            read_model_version=state.read_model_version,
            error_code=state.error_code,
            result=state.result if include_result else None,
        )
