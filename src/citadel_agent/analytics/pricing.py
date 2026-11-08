# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Effective-date pricing selection and conservative Decimal usage-cost estimates."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from citadel_agent.analytics.usage import UsageObservation
from citadel_agent.contracts.common import CitadelModel
from citadel_agent.portal.contracts import TimeRange


class PricingMethod(StrEnum):
    TOKENS = "tokens"
    PERCENTAGE = "percentage"


class PricingStatus(StrEnum):
    PRICED = "priced"
    UNPRICED = "unpriced"


class ModelPricingEntry(CitadelModel):
    """Reviewed pricing document, with optional effective bounds for historical reports."""

    id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=200)
    deployment_name: str = Field(alias="deploymentName", min_length=1, max_length=200)
    active: bool = Field(alias="isActive")
    cost_per_input_unit: Decimal = Field(default=Decimal(0), alias="CostPerInputUnit", ge=0)
    cost_per_output_unit: Decimal = Field(default=Decimal(0), alias="CostPerOutputUnit", ge=0)
    cost_per_cached_input_unit: Decimal = Field(
        default=Decimal(0),
        alias="CostPerCachedInputUnit",
        ge=0,
    )
    cost_per_audio_input_unit: Decimal = Field(
        default=Decimal(0),
        alias="CostPerAudioInputUnit",
        ge=0,
    )
    cost_per_cached_audio_input_unit: Decimal = Field(
        default=Decimal(0),
        alias="CostPerCachedAudioInputUnit",
        ge=0,
    )
    cost_per_audio_output_unit: Decimal = Field(
        default=Decimal(0),
        alias="CostPerAudioOutputUnit",
        ge=0,
    )
    cost_per_reasoning_output_unit: Decimal = Field(
        default=Decimal(0),
        alias="CostPerReasoningOutputUnit",
        ge=0,
    )
    cost_per_image_input_unit: Decimal = Field(
        default=Decimal(0),
        alias="CostPerImageInputUnit",
        ge=0,
    )
    cost_per_cached_image_input_unit: Decimal = Field(
        default=Decimal(0),
        alias="CostPerCachedImageInputUnit",
        ge=0,
    )
    cost_unit: Decimal = Field(alias="CostUnit", gt=0)
    base_cost: Decimal = Field(default=Decimal(0), alias="BaseCost", ge=0)
    currency: str = Field(alias="Currency", pattern=r"^[A-Z]{3}$")
    calculation_method: PricingMethod = Field(alias="CalculationMethod")
    region: str = "ALL"
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    allocation_period: TimeRange | None = Field(default=None, alias="AccountingPeriod")

    @field_validator("effective_from", "effective_to")
    @classmethod
    def _effective_dates_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("pricing effective dates must include a timezone")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def _effective_interval_is_ordered(self) -> ModelPricingEntry:
        if self.effective_to is not None and (
            self.effective_from is None or self.effective_to <= self.effective_from
        ):
            raise ValueError("effective_to must follow an effective_from value")
        if not self.region.strip():
            raise ValueError("pricing region must not be empty")
        return self


class UsageCost(CitadelModel):
    """Cost estimate with an explicit price lineage and no invoice implication."""

    status: PricingStatus
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    pricing_id: str | None = None
    effective_at: datetime | None = None
    warning_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _price_fields_match_status(self) -> UsageCost:
        if self.status is PricingStatus.PRICED:
            if self.amount is None or self.currency is None or self.pricing_id is None:
                raise ValueError("priced results require amount, currency and pricing lineage")
        elif self.amount is not None or self.currency is not None or self.pricing_id is not None:
            raise ValueError("unpriced results cannot contain an amount or price lineage")
        return self

    @field_validator("effective_at")
    @classmethod
    def _effective_at_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("cost effective date must include a timezone")
            return value.astimezone(UTC)
        return value


def estimate_event_cost(
    event: UsageObservation,
    prices: list[ModelPricingEntry],
) -> UsageCost:
    """Estimate one request only when its effective rate and billable breakdown are known."""
    price, warning = select_pricing_entry(event, prices)
    if price is None:
        return _unpriced(warning or "price-not-found")
    if price.calculation_method is PricingMethod.PERCENTAGE:
        return _unpriced("percentage-pricing-requires-complete-period-aggregation")

    amount, warnings = _token_cost(event, price)
    if amount is None:
        return _unpriced(*warnings)
    return UsageCost(
        status=PricingStatus.PRICED,
        amount=amount,
        currency=price.currency,
        pricing_id=price.id,
        effective_at=price.effective_from,
    )


def allocate_percentage_costs(
    events: Sequence[UsageObservation],
    price: ModelPricingEntry,
    *,
    window: TimeRange,
    complete_through: datetime | None,
) -> list[UsageCost]:
    """Allocate a fixed price across usage only after a full, matching period is available."""
    if price.calculation_method is not PricingMethod.PERCENTAGE:
        raise ValueError("percentage allocation requires a percentage pricing entry")
    if not events:
        return []
    if len({event.event_id for event in events}) != len(events):
        raise ValueError("percentage allocation requires unique event IDs")
    if any(event.scope != events[0].scope for event in events):
        raise ValueError("percentage allocation cannot combine different hub scopes")
    if any(not window.start <= event.event_time < window.end for event in events):
        raise ValueError("all allocation events must be inside the requested window")
    if complete_through is None or complete_through.tzinfo is None or complete_through.utcoffset() is None:
        return [_unpriced("percentage-period-completeness-unavailable") for _ in events]
    if complete_through.astimezone(UTC) < window.end:
        return [_unpriced("percentage-period-incomplete") for _ in events]
    if (
        price.effective_from is None
        or price.effective_from > window.start
        or (price.effective_to is not None and price.effective_to < window.end)
    ):
        return [_unpriced("percentage-pricing-window-not-covered") for _ in events]
    if any(
        (event.dimensions.deployment or "").casefold() != price.deployment_name.casefold()
        or select_pricing_entry(event, [price])[0] is None
        for event in events
    ):
        return [_unpriced("percentage-price-does-not-match-event") for _ in events]

    weight_field, weight_for_event = percentage_weight(price)
    if weight_field is None:
        return [_unpriced("percentage-weight-configuration-invalid") for _ in events]
    if weight_for_event is None:
        return [_unpriced("percentage-weight-unsupported") for _ in events]
    if any(weight_field not in event.available_usage_fields for event in events):
        return [_unpriced("percentage-weight-data-unavailable") for _ in events]

    weights = {event.event_id: weight_for_event(event) for event in events}
    total_weight = sum(weights.values())
    if total_weight == 0:
        return [_unpriced("percentage-period-has-zero-weight") for _ in events]

    amounts = {event.event_id: Decimal(0) for event in events}
    allocated = Decimal(0)
    weighted_events = sorted(
        (event for event in events if weights[event.event_id] > 0),
        key=lambda event: event.event_id,
    )
    for event in weighted_events[:-1]:
        amount = price.base_cost * Decimal(weights[event.event_id]) / Decimal(total_weight)
        amounts[event.event_id] = amount
        allocated += amount
    last_event = weighted_events[-1]
    amounts[last_event.event_id] = price.base_cost - allocated
    return [
        UsageCost(
            status=PricingStatus.PRICED,
            amount=amounts[event.event_id],
            currency=price.currency,
            pricing_id=price.id,
            effective_at=price.effective_from,
        )
        for event in events
    ]


def percentage_weight(
    price: ModelPricingEntry,
) -> tuple[str | None, Callable[[UsageObservation], int] | None]:
    unit_rates = [
        ("promptTokens", price.cost_per_input_unit, lambda event: event.usage.prompt),
        ("responseTokens", price.cost_per_output_unit, lambda event: event.usage.completion),
        (
            "promptCachedTokens",
            price.cost_per_cached_input_unit,
            lambda event: event.usage.cached_prompt,
        ),
        (
            "promptAudioTokens",
            price.cost_per_audio_input_unit,
            lambda event: event.usage.prompt_audio,
        ),
        ("promptAudioTokens", price.cost_per_cached_audio_input_unit, None),
        (
            "completionAudioTokens",
            price.cost_per_audio_output_unit,
            lambda event: event.usage.completion_audio,
        ),
        (
            "completionReasoningTokens",
            price.cost_per_reasoning_output_unit,
            lambda event: event.usage.completion_reasoning,
        ),
        (
            "promptImageTokens",
            price.cost_per_image_input_unit,
            lambda event: event.usage.prompt_image,
        ),
        (
            "promptCachedImageTokens",
            price.cost_per_cached_image_input_unit,
            lambda event: event.usage.cached_prompt_image,
        ),
    ]
    selected = [
        (field_name, rate, weight_for_event) for field_name, rate, weight_for_event in unit_rates if rate != 0
    ]
    if len(selected) != 1:
        return None, None
    field_name, rate, weight_for_event = selected[0]
    if rate != 1:
        return None, None
    return field_name, weight_for_event


def select_pricing_entry(
    event: UsageObservation,
    prices: list[ModelPricingEntry],
) -> tuple[ModelPricingEntry | None, str | None]:
    dimensions = event.dimensions
    model = (dimensions.model or "").casefold()
    deployment = (dimensions.deployment or "").casefold()
    region = (dimensions.region or "").casefold()
    dated: list[tuple[tuple[int, int], ModelPricingEntry]] = []
    undated_active: list[tuple[tuple[int, int], ModelPricingEntry]] = []

    for price in prices:
        if price.deployment_name.casefold() == deployment and deployment:
            model_rank = 2
        elif (price.model.casefold() == model and model) or (
            price.model.casefold() == deployment and deployment
        ):
            model_rank = 1
        else:
            continue

        price_region = price.region.casefold()
        if price_region == "all":
            region_rank = 0
        elif region and price_region == region:
            region_rank = 1
        else:
            continue

        rank = (model_rank, region_rank)
        if price.effective_from is None:
            if price.effective_to is None and price.active:
                undated_active.append((rank, price))
            continue
        if price.effective_from <= event.event_time and (
            price.effective_to is None or event.event_time < price.effective_to
        ):
            dated.append((rank, price))

    matches = dated or undated_active
    if not matches:
        if any(
            p.active
            and (p.deployment_name.casefold() == deployment or p.model.casefold() == model)
            and p.effective_from is None
            and p.effective_to is None
            for p in prices
        ):
            return None, "pricing-effective-date-missing"
        return None, "no-effective-price"

    highest_rank = max(rank for rank, _ in matches)
    best = [price for rank, price in matches if rank == highest_rank]
    if len(best) != 1:
        return None, "ambiguous-effective-price"
    price = best[0]
    if price.effective_from is None:
        return None, "pricing-effective-date-missing"
    return price, None


def _token_cost(
    event: UsageObservation,
    price: ModelPricingEntry,
) -> tuple[Decimal | None, list[str]]:
    usage = event.usage
    present = event.available_usage_fields
    missing: list[str] = []

    prompt_inputs = [
        (
            "promptCachedTokens",
            usage.cached_prompt,
            price.cost_per_cached_input_unit,
            price.cost_per_input_unit,
        ),
        (
            "promptAudioTokens",
            usage.prompt_audio,
            price.cost_per_audio_input_unit,
            price.cost_per_input_unit,
        ),
        (
            "promptImageTokens",
            usage.prompt_image,
            price.cost_per_image_input_unit,
            price.cost_per_input_unit,
        ),
        (
            "promptCachedImageTokens",
            usage.cached_prompt_image,
            price.cost_per_cached_image_input_unit,
            price.cost_per_input_unit,
        ),
    ]
    completion_inputs = [
        (
            "completionAudioTokens",
            usage.completion_audio,
            price.cost_per_audio_output_unit,
            price.cost_per_output_unit,
        ),
        (
            "completionReasoningTokens",
            usage.completion_reasoning,
            price.cost_per_reasoning_output_unit,
            price.cost_per_output_unit,
        ),
    ]
    prompt_cost, prompt_warnings = _partitioned_cost(
        usage.prompt,
        price.cost_per_input_unit,
        prompt_inputs,
        present,
    )
    completion_cost, completion_warnings = _partitioned_cost(
        usage.completion,
        price.cost_per_output_unit,
        completion_inputs,
        present,
    )
    cached_audio_warning = _cached_audio_warning(event, price)
    missing.extend(prompt_warnings)
    missing.extend(completion_warnings)
    missing.extend(cached_audio_warning)
    if usage.completion_rejected_prediction and "completionRejectedPredictionTokens" in present:
        missing.append("rejected-prediction-pricing-unsupported")
    if missing or prompt_cost is None or completion_cost is None:
        return None, sorted(set(missing or ["token-breakdown-unavailable"]))
    return (prompt_cost + completion_cost) / price.cost_unit, []


def _partitioned_cost(
    total: int,
    base_rate: Decimal,
    subtypes: list[tuple[str, int, Decimal, Decimal]],
    present: frozenset[str],
) -> tuple[Decimal | None, list[str]]:
    observed_subtypes: list[tuple[str, int, Decimal]] = []
    missing: list[str] = []
    for field_name, count, subtype_rate, reference_rate in subtypes:
        if subtype_rate != reference_rate and field_name not in present:
            missing.append(f"missing-{field_name}")
        if field_name in present and count > 0:
            observed_subtypes.append((field_name, count, subtype_rate))
    if missing:
        return None, missing
    if any(count > total for _, count, _ in observed_subtypes):
        return None, ["token-subtype-exceeds-total"]
    if len(observed_subtypes) > 1 and any(rate != base_rate for _, _, rate in observed_subtypes):
        return None, ["overlapping-token-components-ambiguous"]
    if not observed_subtypes or all(rate == base_rate for _, _, rate in observed_subtypes):
        return Decimal(total) * base_rate, []
    _, count, subtype_rate = observed_subtypes[0]
    return Decimal(total - count) * base_rate + Decimal(count) * subtype_rate, []


def _cached_audio_warning(
    event: UsageObservation,
    price: ModelPricingEntry,
) -> list[str]:
    if price.cost_per_cached_audio_input_unit == price.cost_per_audio_input_unit:
        return []
    if "promptAudioTokens" not in event.available_usage_fields:
        return ["missing-promptAudioTokens"]
    if event.usage.prompt_audio:
        return ["cached-audio-breakdown-unavailable"]
    return []


def _unpriced(*warning_codes: str) -> UsageCost:
    return UsageCost(
        status=PricingStatus.UNPRICED,
        warning_codes=sorted(set(warning_codes)),
    )
