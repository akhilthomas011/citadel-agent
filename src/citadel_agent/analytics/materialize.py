# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Incremental source refreshes, immutable UTC read models and evidence-gated PTU allocation.

Changed source documents replace pseudonymous event rows. Full reconciliations
start from an empty generation, so hard deletes and repairs cannot leave stale
events indefinitely. Neither source exhaustion nor a scheduled run asserts an
ingestion watermark.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from uuid import uuid4

from azure.core.exceptions import AzureError
from pydantic import ValidationError

from citadel_agent.analytics.pricing import (
    ModelPricingEntry,
    PricingMethod,
    PricingStatus,
    UsageCost,
    estimate_event_cost,
    percentage_weight,
    select_pricing_entry,
)
from citadel_agent.analytics.rollups import FinOpsCheckpoint, IncrementalFinOps, RollupGrain, UsageDimension
from citadel_agent.analytics.snapshots import (
    LEASE_SECONDS,
    PAGE_SIZE,
    SNAPSHOT_LIFETIME,
    AllocationTotals,
    AnalyticsConflict,
    AnalyticsDocument,
    AnalyticsHead,
    AnalyticsLimitExceeded,
    AnalyticsStore,
    AnalyticsUnavailable,
    AnalyticsVersion,
    EventRecord,
    GenerationState,
    PeriodCompletenessProof,
    PeriodTotals,
    PriceSnapshot,
    RollupRecord,
    SnapshotInfo,
    add_digest,
    canonical_hash,
    period_key,
    pricing_fingerprint,
    source_fingerprint,
)
from citadel_agent.analytics.usage import UsageNormalizationError, UsageObservation
from citadel_agent.azure_clients.cosmos import LLM_USAGE_CONTAINER
from citadel_agent.contracts.common import CitadelModel
from citadel_agent.portal.contracts import TimeRange
from citadel_agent.portal.data import PortalDataService

logger = logging.getLogger(__name__)
EVENTS_SQL = 'SELECT * FROM c WHERE c.kind IN ("event", "quarantine") ORDER BY c.id'
QUANTUM = Decimal("1e-28")
MAX_PERIODS = 24
MAX_EVENT_BYTES = 64_000
ALL_DIMENSIONS = tuple(UsageDimension)


def _price_key(price: ModelPricingEntry) -> str:
    return canonical_hash(price.model_dump(by_alias=True, mode="json"))


def _unpriced(code: str) -> UsageCost:
    return UsageCost(status=PricingStatus.UNPRICED, warning_codes=[code])


def _rollup_id(checkpoint: FinOpsCheckpoint) -> str:
    bucket = checkpoint.buckets[0]
    return f"{checkpoint.grain.value}:" + canonical_hash(
        [bucket.window.model_dump(mode="json"), list(bucket.dimensions)]
    )


class AnalyticsMaterializer:
    """A bounded resumable worker. Every generation data/cursor write is one fenced batch."""

    def __init__(
        self,
        store: AnalyticsStore,
        data: PortalDataService,
        *,
        owner: str | None = None,
        retention_days: int = 93,
        refresh_interval: timedelta = timedelta(minutes=5),
        reconciliation_interval: timedelta = timedelta(days=1),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if store.scope != data.config.scope:
            raise ValueError("analytics source and storage must share the same hub scope")
        if not 1 <= retention_days <= 366 or refresh_interval <= timedelta(0):
            raise ValueError("analytics retention and refresh intervals must be positive and bounded")
        if reconciliation_interval < refresh_interval:
            raise ValueError("full reconciliation cannot be less frequent than refresh")
        self.store = store
        self.data = data
        self.owner = owner or uuid4().hex
        self.retention_days = retention_days
        self.refresh_interval = refresh_interval
        self.reconciliation_interval = reconciliation_interval
        self.clock = clock
        self.fingerprint = source_fingerprint(data.config)
        self._price_cache: tuple[str, PriceSnapshot] | None = None
        self._eligibility: dict[tuple[str, str], str | None] = {}

    def step(self, *, force_full: bool = False) -> str:
        now = self.clock()
        root = self.store.maybe_get("head")
        if root is None:
            root = self.store.create(self.store.make("head", "head", AnalyticsHead()))
        head = self.store.payload(root, AnalyticsHead)
        if head.active is None:
            previous = head.published
            if (
                not force_full
                and previous is not None
                and previous.source_fingerprint == self.fingerprint
                and now - previous.created_at < self.refresh_interval
            ):
                return "idle"
            full = (
                force_full
                or previous is None
                or previous.expires_at <= now
                or previous.source_fingerprint != self.fingerprint
                or now - previous.full_reconciliation_at >= self.reconciliation_interval
            )
            info = SnapshotInfo(
                generation=canonical_hash(uuid4().hex),
                source_fingerprint=self.fingerprint,
                window=TimeRange(start=now - timedelta(days=self.retention_days), end=now),
                source_cutoff=int(now.timestamp()),
                created_at=now,
                expires_at=now + SNAPSHOT_LIFETIME,
                full_reconciliation_at=now if full else previous.full_reconciliation_at,
                warnings=["source-read-is-not-transactional-snapshot"],
            )
            head.active = GenerationState(info=info, parent=None if full else previous)
            self.store.replace(root, head)
        descriptor = head.active
        if descriptor is None:
            raise AnalyticsUnavailable("Analytics generation creation did not persist.")
        if descriptor.info.expires_at <= now or descriptor.info.source_fingerprint != self.fingerprint:
            head.active = None
            head.last_error_code = "analytics-generation-expired-or-source-changed"
            self.store.replace(root, head)
            return "failed"
        current = self.store.maybe_get("generation", descriptor.info.generation)
        if current is None:
            current = self.store.create(
                self.store.make(
                    "generation",
                    "generation",
                    descriptor,
                    generation=descriptor.info.generation,
                    expires_at=descriptor.info.expires_at,
                )
            )
        state = self.store.payload(current, GenerationState)
        if state.phase == "ready":
            return self._publish(state)
        if state.phase == "failed":
            root = self.store.get("head")
            head = self.store.payload(root, AnalyticsHead)
            if head.active is None or head.active.info.generation != state.info.generation:
                raise AnalyticsConflict("A different analytics generation is active.")
            head.active = None
            head.last_error_code = state.failure_code
            self.store.replace(root, head)
            return "failed"
        if state.lease_owner != self.owner and state.lease_until is not None and state.lease_until > now:
            return "busy"
        state.lease_owner = self.owner
        state.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        current = self.store.replace(current, state)
        try:
            {
                "prices": self._prices,
                "seed": self._seed,
                "source": self._source,
                "stats": self._stats,
                "allocate": self._allocate,
                "remainder": self._remainder,
            }[state.phase](current, state)
        except AnalyticsConflict:
            return "busy"
        except (AnalyticsLimitExceeded, ValidationError, ValueError) as exc:
            logger.error("Analytics generation failed validation/capacity (%s).", type(exc).__name__)
            latest = self.store.get("generation", state.info.generation)
            failed = self.store.payload(latest, GenerationState)
            if failed.lease_owner != self.owner or failed.phase in {"ready", "failed"}:
                return "busy"
            failed.phase = "failed"
            failed.failure_code = (
                "analytics-capacity-limit"
                if isinstance(exc, AnalyticsLimitExceeded)
                else "analytics-invalid-data"
            )
            self.store.commit(latest, failed)
            return "failed"
        return "progress"

    def _record(
        self, state: GenerationState, kind: str, identity: str, payload: CitadelModel
    ) -> AnalyticsDocument:
        return self.store.make(
            kind, identity, payload, generation=state.info.generation, expires_at=state.info.expires_at
        )

    def release(self) -> None:
        root = self.store.maybe_get("head")
        if root is None:
            return
        head = self.store.payload(root, AnalyticsHead)
        if head.active is None:
            return
        current = self.store.get("generation", head.active.info.generation)
        state = self.store.payload(current, GenerationState)
        if state.lease_owner == self.owner and state.phase not in {"ready", "failed"}:
            state.lease_owner = None
            state.lease_until = None
            self.store.replace(current, state)

    def _prices(self, current: AnalyticsVersion, state: GenerationState) -> None:
        prices, warnings = self.data.pricing_snapshot()
        proofs, proof_warnings = self._proofs()
        snapshot = PriceSnapshot(
            prices=prices,
            fingerprint=pricing_fingerprint(prices),
            captured_at=self.clock(),
            proofs=proofs,
            warnings=list(dict.fromkeys([*warnings, *proof_warnings])),
        )
        state.info.pricing_snapshot_at = snapshot.captured_at
        state.info.warnings.extend(snapshot.warnings)
        if state.parent is not None:
            state.info.warnings.append("hard-deletes-await-next-full-reconciliation")
        state.feed_ranges = list(
            self.data.usage.read_feed_ranges(self.data.config.usage_database, LLM_USAGE_CONTAINER)
        )
        if not state.feed_ranges:
            raise AnalyticsUnavailable("The usage source returned no routing ranges.")
        state.phase = "seed" if state.parent is not None else "source"
        self.store.commit(current, state, [self._record(state, "prices", "prices", snapshot)])
        self._price_cache = (state.info.generation, snapshot)

    def _proofs(self) -> tuple[list[PeriodCompletenessProof], list[str]]:
        container = self.data.config.completeness_container
        if container is None:
            return [], ["pipeline-completeness-not-configured"]
        proofs: list[PeriodCompletenessProof] = []
        warnings: list[str] = []
        try:
            rows, truncated = self.data._scan(
                container,
                "SELECT * FROM c WHERE c.scope.hubId = @hub",
                {"hub": self.store.scope.hub_id},
                2000,
            )
            if truncated:
                return [], ["pipeline-completeness-scan-limit"]
            names = set(PeriodCompletenessProof.model_fields)
            names.update(
                field.alias for field in PeriodCompletenessProof.model_fields.values() if field.alias
            )
            for row in rows:
                try:
                    proof = PeriodCompletenessProof.model_validate(
                        {name: value for name, value in row.items() if name in names}
                    )
                except ValidationError:
                    warnings.append("invalid-pipeline-completeness-records")
                    continue
                if proof.scope == self.store.scope and proof.issued_at <= self.clock():
                    proofs.append(proof)
        except AzureError:
            logger.warning("Trusted pipeline completeness source is unavailable.")
            return [], ["pipeline-completeness-source-unavailable"]
        return proofs, list(dict.fromkeys(warnings))

    def _seed(self, current: AnalyticsVersion, state: GenerationState) -> None:
        parent = state.parent
        if parent is None or parent.expires_at <= self.clock():
            raise ValueError("The parent snapshot expired; a full reconciliation is required.")
        page = self.store.page(parent.generation, EVENTS_SQL, {}, continuation=state.continuation)
        if parent.expires_at <= self.clock():
            raise ValueError("The parent snapshot expired during its copy.")
        records = []
        for raw in page.items:
            old = self.store.decode(raw)
            event = self.store.payload(old, EventRecord)
            if event.observation is not None:
                if not state.info.window.start <= event.observation.event_time < state.info.window.end:
                    continue
                event.cost = None
            records.append(self._record(state, old.document.kind, old.document.id, event))
        state.continuation = page.continuation_token
        if state.continuation is None:
            state.phase = "source"
        self.store.commit(current, state, records)

    def _source(self, current: AnalyticsVersion, state: GenerationState) -> None:
        floor = max(0, state.parent.source_cutoff - 1) if state.parent else 0
        page = self.data.usage.query_page(
            self.data.config.usage_database,
            LLM_USAGE_CONTAINER,
            "SELECT * FROM c WHERE c.gatewayName = @gateway AND c._ts >= @floor AND c._ts <= @cutoff",
            {"gateway": self.data.config.apim_name, "floor": floor, "cutoff": state.info.source_cutoff},
            page_size=PAGE_SIZE,
            continuation_token=state.continuation,
            feed_range=state.feed_ranges[state.range_index],
        )
        if len(page.items) > PAGE_SIZE or (
            page.continuation_token is not None and page.continuation_token == state.continuation
        ):
            raise AnalyticsUnavailable("Source pagination did not make bounded progress.")
        records: dict[str, AnalyticsDocument] = {}
        for index, document in enumerate(page.items):
            if document.get("gatewayName") != self.data.config.apim_name:
                continue
            identity = "q:" + canonical_hash([state.range_index, state.continuation, index])
            try:
                identity = "e:" + self.data.source_key(document).event_id
                event = self.data.normalize(document)
                observation = UsageObservation.model_validate(event.model_dump(exclude={"source_key"}))
                if len(observation.model_dump_json().encode()) > MAX_EVENT_BYTES:
                    raise UsageNormalizationError("normalized-metadata-too-large", "dimensions")
            except (UsageNormalizationError, ValidationError, ValueError, KeyError) as exc:
                reason = exc.code if isinstance(exc, UsageNormalizationError) else "invalid-usage-record"
                records[identity] = self._record(state, "quarantine", identity, EventRecord(reason=reason))
                continue
            inside = state.info.window.start <= observation.event_time < state.info.window.end
            records[identity] = self._record(
                state,
                "event" if inside else "excluded",
                identity,
                EventRecord(observation=observation) if inside else EventRecord(reason="outside-retention"),
            )
        state.source_documents += len(page.items)
        state.continuation = page.continuation_token
        if state.continuation is None:
            state.range_index += 1
            if state.range_index == len(state.feed_ranges):
                state.phase = "stats"
        self.store.commit(current, state, list(records.values()))

    def _catalog(self, state: GenerationState) -> PriceSnapshot:
        if self._price_cache is None or self._price_cache[0] != state.info.generation:
            snapshot = self.store.payload(self.store.get("prices", state.info.generation), PriceSnapshot)
            self._price_cache = (state.info.generation, snapshot)
            self._eligibility.clear()
        return self._price_cache[1]

    @staticmethod
    def _periods(prices: PriceSnapshot, state: GenerationState) -> dict[str, TimeRange]:
        windows = {
            period_key(price.allocation_period): price.allocation_period
            for price in prices.prices
            if price.calculation_method is PricingMethod.PERCENTAGE
            and price.allocation_period is not None
            and state.info.window.start <= price.allocation_period.start
            and price.allocation_period.end <= state.info.window.end
        }
        if len(windows) > MAX_PERIODS:
            if "percentage-accounting-period-capacity-limited" not in state.info.warnings:
                state.info.warnings.append("percentage-accounting-period-capacity-limited")
            return {}
        for proof in sorted(prices.proofs, key=lambda value: value.window.end, reverse=True):
            if (
                proof.source_fingerprint != state.info.source_fingerprint
                or proof.pricing_fingerprint != prices.fingerprint
                or not state.info.window.start
                <= proof.window.start
                < proof.window.end
                <= state.info.window.end
            ):
                continue
            key = period_key(proof.window)
            if key not in windows and len(windows) == MAX_PERIODS:
                if "pipeline-proof-window-capacity-limited" not in state.info.warnings:
                    state.info.warnings.append("pipeline-proof-window-capacity-limited")
                continue
            windows[key] = proof.window
        return windows

    def _stats(self, current: AnalyticsVersion, state: GenerationState) -> None:
        catalog = self._catalog(state)
        periods = self._periods(catalog, state)
        page = self.store.page(state.info.generation, EVENTS_SQL, {}, continuation=state.continuation)
        period_rows: dict[str, PeriodTotals] = {}
        allocations: dict[str, AllocationTotals] = {}
        for raw in page.items:
            version = self.store.decode(raw)
            record = self.store.payload(version, EventRecord)
            event = record.observation
            if event is None:
                state.info.rejected_records += 1
                continue
            state.info.event_count += 1
            for key, window in periods.items():
                if not window.start <= event.event_time < window.end:
                    continue
                if key not in period_rows:
                    previous = self.store.maybe_get("p:" + key, state.info.generation)
                    period_rows[key] = (
                        self.store.payload(previous, PeriodTotals)
                        if previous
                        else PeriodTotals(window=window)
                    )
                totals = period_rows[key]
                totals.event_count += 1
                totals.event_digest = add_digest(totals.event_digest, event)
            price, _ = select_pricing_entry(event, catalog.prices)
            if price is None or price.calculation_method is not PricingMethod.PERCENTAGE:
                continue
            key = _price_key(price)
            if key not in allocations:
                previous = self.store.maybe_get("a:" + key, state.info.generation)
                allocations[key] = (
                    self.store.payload(previous, AllocationTotals)
                    if previous
                    else AllocationTotals(price_id=price.id)
                )
            totals = allocations[key]
            if (
                price.allocation_period is None
                or not price.allocation_period.start <= event.event_time < price.allocation_period.end
                or (event.dimensions.deployment or "").casefold() != price.deployment_name.casefold()
            ):
                continue
            field, weight = percentage_weight(price)
            if field is None or weight is None or field not in event.available_usage_fields:
                totals.unavailable = True
            else:
                value = weight(event)
                totals.weight += value
                if value:
                    totals.last_event = max(totals.last_event or "", event.event_id)
        records = [self._record(state, "period", "p:" + key, value) for key, value in period_rows.items()]
        records.extend(
            self._record(state, "allocation", "a:" + key, value) for key, value in allocations.items()
        )
        state.continuation = page.continuation_token
        if state.continuation is None:
            state.phase = "allocate"
        self.store.commit(current, state, records)

    def _eligible(
        self,
        state: GenerationState,
        catalog: PriceSnapshot,
        price: ModelPricingEntry,
        totals: AllocationTotals,
    ) -> str | None:
        key = (state.info.generation, _price_key(price))
        if key not in self._eligibility:
            self._eligibility[key] = self._eligibility_reason(state, catalog, price, totals)
        return self._eligibility[key]

    def _eligibility_reason(
        self,
        state: GenerationState,
        catalog: PriceSnapshot,
        price: ModelPricingEntry,
        totals: AllocationTotals,
    ) -> str | None:
        window = price.allocation_period
        if window is None:
            return "percentage-accounting-period-unavailable"
        if period_key(window) not in self._periods(catalog, state):
            return "percentage-period-outside-read-model-coverage"
        if state.info.rejected_records:
            return "percentage-period-contains-quarantined-records"
        if totals.unavailable or totals.weight == 0:
            return "percentage-weight-data-unavailable-or-zero"
        if (
            price.effective_from is None
            or price.effective_from > window.start
            or (price.effective_to is not None and price.effective_to < window.end)
        ):
            return "percentage-pricing-window-not-covered"
        with localcontext() as context:
            context.prec = 100
            if price.base_cost != price.base_cost.quantize(QUANTUM):
                return "percentage-base-cost-precision-unsupported"
        period = self.store.payload(
            self.store.get("p:" + period_key(window), state.info.generation), PeriodTotals
        )
        proofs = [
            proof
            for proof in catalog.proofs
            if proof.scope == self.store.scope
            and proof.window == window
            and proof.source_fingerprint == state.info.source_fingerprint
            and proof.pricing_fingerprint == catalog.fingerprint
            and proof.source_write_cutoff <= state.info.source_cutoff
            and proof.event_count == period.event_count
            and proof.event_digest == period.event_digest
        ]
        if len(proofs) != 1:
            return "percentage-period-completeness-unavailable-or-ambiguous"
        return None

    def _allocate(self, current: AnalyticsVersion, state: GenerationState) -> None:
        catalog = self._catalog(state)
        page = self.store.page(state.info.generation, EVENTS_SQL, {}, continuation=state.continuation)
        event_rows: list[AnalyticsDocument] = []
        allocations: dict[str, AllocationTotals] = {}
        costs: dict[str, UsageCost] = {}
        events: list[UsageObservation] = []
        for raw in page.items:
            version = self.store.decode(raw)
            record = self.store.payload(version, EventRecord)
            event = record.observation
            if event is None:
                continue
            price, _ = select_pricing_entry(event, catalog.prices)
            cost = estimate_event_cost(event, catalog.prices)
            if price is not None and price.calculation_method is PricingMethod.PERCENTAGE:
                key = _price_key(price)
                if key not in allocations:
                    allocations[key] = self.store.payload(
                        self.store.get("a:" + key, state.info.generation), AllocationTotals
                    )
                totals = allocations[key]
                warning = self._eligible(state, catalog, price, totals)
                outside_period = price.allocation_period is not None and not (
                    price.allocation_period.start <= event.event_time < price.allocation_period.end
                )
                different_deployment = (
                    event.dimensions.deployment or ""
                ).casefold() != price.deployment_name.casefold()
                if different_deployment:
                    warning = "percentage-price-does-not-match-deployment"
                elif outside_period:
                    warning = "percentage-event-outside-accounting-period"
                if warning:
                    cost = _unpriced(warning)
                    if not outside_period and not different_deployment:
                        totals.warning_code = warning
                else:
                    _, get_weight = percentage_weight(price)
                    if get_weight is None:
                        raise ValueError("validated percentage weighting is unavailable")
                    with localcontext() as context:
                        context.prec = 100
                        amount = (
                            price.base_cost * Decimal(get_weight(event)) / Decimal(totals.weight)
                        ).quantize(QUANTUM, rounding=ROUND_DOWN)
                        totals.floor_amount += amount
                    cost = UsageCost(
                        status=PricingStatus.PRICED,
                        amount=amount,
                        currency=price.currency,
                        pricing_id=price.id,
                        effective_at=price.effective_from,
                    )
            record.cost = cost
            event_rows.append(self._record(state, "event", version.document.id, record))
            events.append(event)
            costs[event.event_id] = cost
        rollups: list[AnalyticsDocument] = []
        for grain in (RollupGrain.HOUR, RollupGrain.DAY):
            delta = IncrementalFinOps(self.store.scope, state.info.window, ALL_DIMENSIONS, grain=grain)
            delta.add(events, costs)
            for bucket in delta.checkpoint().buckets:
                single = FinOpsCheckpoint(
                    scope=self.store.scope,
                    window=state.info.window,
                    group_by=ALL_DIMENSIONS,
                    buckets=[bucket],
                    grain=grain,
                )
                identity = _rollup_id(single)
                previous = self.store.maybe_get(identity, state.info.generation)
                if previous is not None:
                    target = IncrementalFinOps.restore(self.store.payload(previous, RollupRecord).checkpoint)
                    target.merge(single)
                    single = target.checkpoint()
                rollups.append(self._record(state, grain.value, identity, RollupRecord(checkpoint=single)))
        records = event_rows + rollups
        records.extend(
            self._record(state, "allocation", "a:" + key, value) for key, value in allocations.items()
        )
        state.continuation = page.continuation_token
        if state.continuation is None:
            state.phase = "remainder"
        self.store.commit(current, state, records)

    def _remainder(self, current: AnalyticsVersion, state: GenerationState) -> None:
        catalog = self._catalog(state)
        prices = [value for value in catalog.prices if value.calculation_method is PricingMethod.PERCENTAGE]
        if state.remainder_index == len(prices):
            state.phase = "ready"
            state.continuation = None
            self.store.commit(current, state)
            return
        price = prices[state.remainder_index]
        key = _price_key(price)
        allocation = self.store.maybe_get("a:" + key, state.info.generation)
        records: list[AnalyticsDocument] = []
        if allocation is not None:
            totals = self.store.payload(allocation, AllocationTotals)
            if totals.warning_code is None and totals.last_event is not None:
                event_version = self.store.get("e:" + totals.last_event, state.info.generation)
                record = self.store.payload(event_version, EventRecord)
                event, cost = record.observation, record.cost
                if event is None or cost is None or cost.amount is None:
                    raise ValueError("fixed-period remainder lost its priced event")
                with localcontext() as context:
                    context.prec = 100
                    remainder = price.base_cost - totals.floor_amount
                    if remainder < 0:
                        raise ValueError("fixed-period floors exceed their reviewed BaseCost")
                    cost.amount += remainder
                records.append(self._record(state, "event", event_version.document.id, record))
                for grain in (RollupGrain.HOUR, RollupGrain.DAY):
                    sample = IncrementalFinOps(
                        self.store.scope, state.info.window, ALL_DIMENSIONS, grain=grain
                    )
                    sample.add([event], {event.event_id: cost})
                    identity = _rollup_id(sample.checkpoint())
                    existing = self.store.payload(
                        self.store.get(identity, state.info.generation), RollupRecord
                    )
                    accumulator = IncrementalFinOps.restore(existing.checkpoint)
                    accumulator.adjust_amount(event, price.currency, remainder)
                    records.append(
                        self._record(
                            state, grain.value, identity, RollupRecord(checkpoint=accumulator.checkpoint())
                        )
                    )
            totals.finalized = True
            records.append(self._record(state, "allocation", "a:" + key, totals))
        state.remainder_index += 1
        self.store.commit(current, state, records)

    def _publish(self, state: GenerationState) -> str:
        root = self.store.get("head")
        head = self.store.payload(root, AnalyticsHead)
        if head.published is not None and head.published.generation == state.info.generation:
            return "published"
        if head.active is None or head.active.info.generation != state.info.generation:
            raise AnalyticsConflict("A different analytics generation is active.")
        head.published = state.info
        head.active = None
        head.last_error_code = None
        self.store.replace(root, head)
        return "published"
