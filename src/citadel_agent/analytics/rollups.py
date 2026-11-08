# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Deterministic daily FinOps aggregates over normalized usage events."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum

from pydantic import model_validator

from citadel_agent.analytics.pricing import PricingStatus, UsageCost
from citadel_agent.analytics.usage import UsageObservation
from citadel_agent.contracts.common import CitadelModel
from citadel_agent.portal.contracts import (
    CostBasis,
    DataQuality,
    DataSource,
    FinOpsAggregate,
    HubScope,
    MonetaryAmount,
    ObservationState,
    PricingCoverage,
    TimeRange,
    TokenUsage,
    UsageDimensions,
)


class UsageDimension(StrEnum):
    MODEL = "model"
    DEPLOYMENT = "deployment"
    PRODUCT = "product"
    APPLICATION = "application"
    BACKEND = "backend"
    REGION = "region"
    SERVICE_TYPE = "service_type"
    GATEWAY = "gateway"
    OPERATION_NAME = "operation_name"
    AI_GATEWAY_ID = "ai_gateway_id"
    CUSTOM_DIMENSION_1 = "custom_dimension_1"
    CUSTOM_DIMENSION_2 = "custom_dimension_2"


class RollupGrain(StrEnum):
    HOUR = "hour"
    DAY = "day"
    WINDOW = "window"


COMMON_DIMENSIONS = (
    UsageDimension.MODEL,
    UsageDimension.DEPLOYMENT,
    UsageDimension.PRODUCT,
    UsageDimension.APPLICATION,
    UsageDimension.BACKEND,
    UsageDimension.REGION,
)

_DIMENSION_GETTERS: dict[UsageDimension, Callable[[UsageDimensions], str | None]] = {
    UsageDimension.MODEL: lambda dimensions: dimensions.model,
    UsageDimension.DEPLOYMENT: lambda dimensions: dimensions.deployment,
    UsageDimension.PRODUCT: lambda dimensions: dimensions.product,
    UsageDimension.APPLICATION: lambda dimensions: dimensions.application,
    UsageDimension.BACKEND: lambda dimensions: dimensions.backend,
    UsageDimension.REGION: lambda dimensions: dimensions.region,
    UsageDimension.SERVICE_TYPE: lambda dimensions: dimensions.service_type,
    UsageDimension.GATEWAY: lambda dimensions: dimensions.gateway,
    UsageDimension.OPERATION_NAME: lambda dimensions: dimensions.operation_name,
    UsageDimension.AI_GATEWAY_ID: lambda dimensions: dimensions.ai_gateway_id,
    UsageDimension.CUSTOM_DIMENSION_1: lambda dimensions: dimensions.custom_dimension_1,
    UsageDimension.CUSTOM_DIMENSION_2: lambda dimensions: dimensions.custom_dimension_2,
}


@dataclass(frozen=True)
class _TokenField:
    source_name: str
    target_name: str
    get_value: Callable[[TokenUsage], int | None]
    optional: bool


_TOKEN_FIELDS = (
    _TokenField("promptTokens", "prompt", lambda usage: usage.prompt, False),
    _TokenField("responseTokens", "completion", lambda usage: usage.completion, False),
    _TokenField("totalTokens", "total", lambda usage: usage.total, True),
    _TokenField("promptCachedTokens", "cached_prompt", lambda usage: usage.cached_prompt, True),
    _TokenField("promptAudioTokens", "prompt_audio", lambda usage: usage.prompt_audio, True),
    _TokenField("completionAudioTokens", "completion_audio", lambda usage: usage.completion_audio, True),
    _TokenField(
        "completionReasoningTokens",
        "completion_reasoning",
        lambda usage: usage.completion_reasoning,
        True,
    ),
    _TokenField(
        "completionAcceptedPredictionTokens",
        "completion_accepted_prediction",
        lambda usage: usage.completion_accepted_prediction,
        True,
    ),
    _TokenField(
        "completionRejectedPredictionTokens",
        "completion_rejected_prediction",
        lambda usage: usage.completion_rejected_prediction,
        True,
    ),
    _TokenField("promptImageTokens", "prompt_image", lambda usage: usage.prompt_image, True),
    _TokenField(
        "promptCachedImageTokens",
        "cached_prompt_image",
        lambda usage: usage.cached_prompt_image,
        True,
    ),
)

TOKEN_SOURCE_FIELDS = {item.target_name: item.source_name for item in _TOKEN_FIELDS}


@dataclass
class _RollupBucket:
    dimensions: tuple[str | None, ...]
    window: TimeRange
    usage_totals: dict[str, int] = field(
        default_factory=lambda: {item.target_name: 0 for item in _TOKEN_FIELDS}
    )
    total_tokens_complete: bool = True
    total_usage_weight: int = 0
    priced_usage_weight: int = 0
    amounts_by_currency: dict[str, Decimal] = field(default_factory=dict)
    pricing_dates_by_currency: dict[str, set[datetime]] = field(default_factory=dict)
    undated_priced_currencies: set[str] = field(default_factory=set)
    unpriced_dimensions: set[str] = field(default_factory=set)
    pricing_warning_codes: set[str] = field(default_factory=set)
    usage_warning_codes: set[str] = field(default_factory=set)
    requests: int = 0
    unpriced_requests: int = 0


class FinOpsCheckpoint(CitadelModel):
    """Serializable aggregate state; source records and continuation tokens do not belong here."""

    scope: HubScope
    window: TimeRange
    group_by: tuple[UsageDimension, ...]
    buckets: list[_RollupBucket]
    grain: RollupGrain = RollupGrain.DAY

    @model_validator(mode="after")
    def _consistent_buckets(self) -> FinOpsCheckpoint:
        if len(set(self.group_by)) != len(self.group_by):
            raise ValueError("group_by dimensions must be unique")
        keys = set()
        expected_fields = {item.target_name for item in _TOKEN_FIELDS}
        for bucket in self.buckets:
            key = (bucket.window.start, bucket.window.end, bucket.dimensions)
            if key in keys or len(bucket.dimensions) != len(self.group_by):
                raise ValueError("checkpoint buckets must have unique, matching dimension keys")
            keys.add(key)
            if not self.window.start <= bucket.window.start < bucket.window.end <= self.window.end:
                raise ValueError("checkpoint buckets must be inside the reporting window")
            if bucket.window != _bucket_window(bucket.window.start, self.window, self.grain):
                raise ValueError(
                    "checkpoint buckets must use the requested UTC-day boundaries or UTC-hour boundaries"
                )
            if not 0 <= bucket.unpriced_requests <= bucket.requests:
                raise ValueError("checkpoint request counters are inconsistent")
            if set(bucket.usage_totals) != expected_fields or any(
                value < 0 for value in bucket.usage_totals.values()
            ):
                raise ValueError("checkpoint token totals must be complete and non-negative")
            if not 0 <= bucket.priced_usage_weight <= bucket.total_usage_weight:
                raise ValueError("checkpoint pricing weights are inconsistent")
            if bucket.total_usage_weight != bucket.usage_totals["prompt"] + bucket.usage_totals["completion"]:
                raise ValueError(
                    "checkpoint pricing weight must match observed prompt plus completion tokens"
                )
            if any(value < 0 or not value.is_finite() for value in bucket.amounts_by_currency.values()):
                raise ValueError("checkpoint money must be finite and non-negative")
        return self


class IncrementalFinOps:
    """Accumulate each committed source page without rounding money or retaining raw events.

    The caller must checkpoint this state atomically with source progress. Event
    identifiers are checked within a batch; cross-page retry safety belongs to
    the checkpoint/ETag boundary, not an unbounded in-memory set of every ID.
    """

    def __init__(
        self,
        scope: HubScope,
        window: TimeRange,
        group_by: tuple[UsageDimension, ...] = COMMON_DIMENSIONS,
        *,
        grain: RollupGrain = RollupGrain.DAY,
    ) -> None:
        if len(set(group_by)) != len(group_by):
            raise ValueError("group_by dimensions must be unique")
        self.scope = scope
        self.window = window
        self.group_by = group_by
        self.grain = grain
        self._buckets: dict[tuple[datetime, datetime, tuple[str | None, ...]], _RollupBucket] = {}

    @classmethod
    def restore(cls, checkpoint: FinOpsCheckpoint) -> IncrementalFinOps:
        result = cls(checkpoint.scope, checkpoint.window, checkpoint.group_by, grain=checkpoint.grain)
        for bucket in checkpoint.model_copy(deep=True).buckets:
            result._buckets[(bucket.window.start, bucket.window.end, bucket.dimensions)] = bucket
        return result

    def checkpoint(self) -> FinOpsCheckpoint:
        return FinOpsCheckpoint(
            scope=self.scope,
            window=self.window,
            group_by=self.group_by,
            buckets=[bucket for _, bucket in sorted(self._buckets.items(), key=_bucket_sort_key)],
            grain=self.grain,
        ).model_copy(deep=True)

    @property
    def bucket_count(self) -> int:
        return len(self._buckets)

    @property
    def usage_weight(self) -> int:
        return sum(bucket.total_usage_weight for bucket in self._buckets.values())

    @property
    def priced_weight(self) -> int:
        return sum(bucket.priced_usage_weight for bucket in self._buckets.values())

    def add(self, events: Sequence[UsageObservation], event_costs: Mapping[str, UsageCost]) -> None:
        if len({event.event_id for event in events}) != len(events):
            raise ValueError("usage events must have unique event IDs")
        if any(event.scope != self.scope for event in events):
            raise ValueError("a FinOps aggregate cannot combine different hub scopes")
        if any(not self.window.start <= event.event_time < self.window.end for event in events):
            raise ValueError("all usage events must be inside the requested window")
        if set(event_costs) != {event.event_id for event in events}:
            raise ValueError("event costs must match the usage event IDs exactly")
        for event in events:
            window = _bucket_window(event.event_time, self.window, self.grain)
            dimensions = tuple(_DIMENSION_GETTERS[dimension](event.dimensions) for dimension in self.group_by)
            bucket = self._buckets.setdefault(
                (window.start, window.end, dimensions), _RollupBucket(dimensions=dimensions, window=window)
            )
            _add_usage(bucket, event)
            _add_cost(bucket, event, event_costs[event.event_id], self.group_by)
            bucket.requests += 1
            bucket.unpriced_requests += int(event_costs[event.event_id].status is PricingStatus.UNPRICED)

    def merge(self, checkpoint: FinOpsCheckpoint) -> None:
        """Merge complete finer-grained buckets, never prorating token or monetary totals."""
        if checkpoint.scope != self.scope:
            raise ValueError("rollup merge cannot combine hub scopes")
        if not set(self.group_by) <= set(checkpoint.group_by):
            raise ValueError("rollup merge requires every requested attribution dimension")
        indexes = [checkpoint.group_by.index(value) for value in self.group_by]
        for source in checkpoint.buckets:
            if not self.window.start <= source.window.start < source.window.end <= self.window.end:
                raise ValueError("rollup merge cannot prorate boundary buckets")
            target_window = _bucket_window(source.window.start, self.window, self.grain)
            if source.window.end > target_window.end:
                raise ValueError("rollup merge cannot split a coarser bucket")
            dimensions = tuple(source.dimensions[index] for index in indexes)
            target = self._buckets.setdefault(
                (target_window.start, target_window.end, dimensions),
                _RollupBucket(dimensions=dimensions, window=target_window),
            )
            for name, value in source.usage_totals.items():
                target.usage_totals[name] += value
            target.total_tokens_complete &= source.total_tokens_complete
            target.total_usage_weight += source.total_usage_weight
            target.priced_usage_weight += source.priced_usage_weight
            target.requests += source.requests
            target.unpriced_requests += source.unpriced_requests
            with localcontext() as context:
                context.prec = 100
                for currency, amount in source.amounts_by_currency.items():
                    target.amounts_by_currency[currency] = (
                        target.amounts_by_currency.get(currency, Decimal(0)) + amount
                    )
            for currency, dates in source.pricing_dates_by_currency.items():
                target.pricing_dates_by_currency.setdefault(currency, set()).update(dates)
            target.undated_priced_currencies.update(source.undated_priced_currencies)
            if source.unpriced_dimensions or source.unpriced_requests:
                target.unpriced_dimensions.add(
                    _unpriced_dimension_label(
                        UsageDimensions(**dict(zip(self.group_by, dimensions, strict=True)))
                    )
                )
            target.pricing_warning_codes.update(source.pricing_warning_codes)
            target.usage_warning_codes.update(source.usage_warning_codes)

    def adjust_amount(self, event: UsageObservation, currency: str, amount: Decimal) -> None:
        """Apply an exact fixed-period remainder without changing usage or price coverage."""
        window = _bucket_window(event.event_time, self.window, self.grain)
        dimensions = tuple(_DIMENSION_GETTERS[value](event.dimensions) for value in self.group_by)
        bucket = self._buckets[(window.start, window.end, dimensions)]
        if amount < 0 or currency not in bucket.amounts_by_currency:
            raise ValueError("a remainder requires an existing priced currency and a non-negative amount")
        with localcontext() as context:
            context.prec = 100
            bucket.amounts_by_currency[currency] += amount

    def results(self, quality: DataQuality) -> list[FinOpsAggregate]:
        if quality.source is not DataSource.COSMOS_USAGE or quality.event_window != self.window:
            raise ValueError("quality metadata must describe the requested Cosmos usage window")
        return [
            _build_aggregate(bucket, self.scope, quality, self.group_by)
            for _, bucket in sorted(self._buckets.items(), key=_bucket_sort_key)
        ]


def aggregate_finops_events(
    events: Sequence[UsageObservation],
    event_costs: Mapping[str, UsageCost],
    *,
    window: TimeRange,
    quality: DataQuality,
    group_by: tuple[UsageDimension, ...] = COMMON_DIMENSIONS,
) -> list[FinOpsAggregate]:
    """Build deterministic UTC-day aggregates without dropping unpriced usage."""
    if quality.source is not DataSource.COSMOS_USAGE or quality.event_window != window:
        raise ValueError("quality metadata must describe the requested Cosmos usage window")
    if len(set(group_by)) != len(group_by):
        raise ValueError("group_by dimensions must be unique")
    if not events:
        if event_costs:
            raise ValueError("event costs cannot be supplied without usage events")
        return []
    accumulator = IncrementalFinOps(events[0].scope, window, group_by)
    accumulator.add(events, event_costs)
    return accumulator.results(quality)


def _daily_window(event_time: datetime, requested_window: TimeRange) -> TimeRange:
    return _bucket_window(event_time, requested_window, RollupGrain.DAY)


def _bucket_window(event_time: datetime, requested_window: TimeRange, grain: RollupGrain) -> TimeRange:
    if grain is RollupGrain.WINDOW:
        return requested_window
    day_start = event_time.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if grain is RollupGrain.DAY:
        day_start = day_start.replace(hour=0)
    day_end = day_start + (timedelta(days=1) if grain is RollupGrain.DAY else timedelta(hours=1))
    return TimeRange(
        start=max(day_start, requested_window.start),
        end=min(day_end, requested_window.end),
    )


def _add_usage(bucket: _RollupBucket, event: UsageObservation) -> None:
    for token_field in _TOKEN_FIELDS:
        if token_field.source_name not in event.available_usage_fields:
            if not token_field.optional:
                raise ValueError(f"required usage field '{token_field.source_name}' is unavailable")
            bucket.usage_warning_codes.add(f"usage-field-unavailable:{token_field.source_name}")
            if token_field.target_name == "total":
                bucket.total_tokens_complete = False
            continue

        count = token_field.get_value(event.usage)
        if count is None:
            if token_field.target_name == "total":
                bucket.total_tokens_complete = False
                bucket.usage_warning_codes.add("usage-field-unavailable:totalTokens")
                continue
            raise ValueError(f"available usage field '{token_field.source_name}' has no value")
        bucket.usage_totals[token_field.target_name] += count

    bucket.total_usage_weight += event.usage.prompt + event.usage.completion


def _add_cost(
    bucket: _RollupBucket, event: UsageObservation, cost: UsageCost, group_by: tuple[UsageDimension, ...]
) -> None:
    if cost.status is PricingStatus.UNPRICED:
        bucket.unpriced_dimensions.add(
            _unpriced_dimension_label(
                UsageDimensions(**{dimension: getattr(event.dimensions, dimension) for dimension in group_by})
            )
        )
        bucket.pricing_warning_codes.update(cost.warning_codes)
        return
    if cost.amount is None or cost.currency is None:
        raise ValueError("priced usage costs must include an amount and currency")
    if cost.amount < 0:
        raise ValueError("priced usage costs must not be negative")

    with localcontext() as context:
        context.prec = 100
        bucket.amounts_by_currency[cost.currency] = (
            bucket.amounts_by_currency.get(cost.currency, Decimal(0)) + cost.amount
        )
    bucket.priced_usage_weight += event.usage.prompt + event.usage.completion
    if cost.effective_at is None:
        bucket.undated_priced_currencies.add(cost.currency)
    else:
        bucket.pricing_dates_by_currency.setdefault(cost.currency, set()).add(cost.effective_at)


def _unpriced_dimension_label(dimensions: UsageDimensions) -> str:
    values = dimensions.model_dump(mode="python", exclude_none=True)
    if not values:
        return "unattributed"
    return ";".join(f"{key}={values[key]}" for key in sorted(values))


def _build_aggregate(
    bucket: _RollupBucket,
    scope: HubScope,
    source_quality: DataQuality,
    group_by: tuple[UsageDimension, ...],
) -> FinOpsAggregate:
    coverage_warnings = set(bucket.pricing_warning_codes)
    if bucket.total_usage_weight == 0:
        priced_usage_percent = Decimal(0)
        coverage_warnings.add("pricing-coverage-zero-token-volume")
    else:
        priced_usage_percent = (
            Decimal(100) * Decimal(bucket.priced_usage_weight) / Decimal(bucket.total_usage_weight)
        )

    pricing_dates = set().union(*bucket.pricing_dates_by_currency.values())
    coverage_effective_at = (
        next(iter(pricing_dates))
        if len(pricing_dates) == 1 and not bucket.undated_priced_currencies
        else None
    )
    costs = []
    for currency, amount in sorted(bucket.amounts_by_currency.items()):
        dates = bucket.pricing_dates_by_currency.get(currency, set())
        effective_at = (
            next(iter(dates))
            if len(dates) == 1 and currency not in bucket.undated_priced_currencies
            else None
        )
        costs.append(
            MonetaryAmount(
                amount=amount,
                currency=currency,
                basis=CostBasis.USAGE_ESTIMATE,
                source=DataSource.COSMOS_USAGE,
                effective_at=effective_at,
            )
        )

    quality_warnings = list(
        dict.fromkeys((*source_quality.warning_codes, *sorted(bucket.usage_warning_codes)))
    )
    quality_state = source_quality.state
    if bucket.usage_warning_codes and quality_state is ObservationState.HEALTHY:
        quality_state = ObservationState.PARTIAL
    aggregate_quality = DataQuality(
        source=source_quality.source,
        observed_at=source_quality.observed_at,
        event_window=bucket.window,
        state=quality_state,
        watermark=source_quality.watermark,
        warning_codes=quality_warnings,
    )
    usage_values = dict(bucket.usage_totals)
    usage_values["total"] = bucket.usage_totals["total"] if bucket.total_tokens_complete else None
    return FinOpsAggregate(
        scope=scope,
        window=bucket.window,
        dimensions=UsageDimensions(
            **{dimension.value: value for dimension, value in zip(group_by, bucket.dimensions, strict=True)}
        ),
        usage=TokenUsage(**usage_values),
        costs=costs,
        pricing_coverage=PricingCoverage(
            priced_usage_percent=priced_usage_percent,
            unpriced_dimensions=sorted(bucket.unpriced_dimensions),
            warning_codes=sorted(coverage_warnings),
            pricing_effective_at=coverage_effective_at,
        ),
        quality=aggregate_quality,
    )


def _bucket_sort_key(
    item: tuple[tuple[datetime, datetime, tuple[str | None, ...]], _RollupBucket],
) -> tuple[datetime, tuple[tuple[bool, str], ...]]:
    key, _ = item
    return key[0], tuple((value is None, value or "") for value in key[2])
