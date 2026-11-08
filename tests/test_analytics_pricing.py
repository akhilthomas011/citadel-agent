"""Usage estimates use effective-dated prices and Decimal without overlap assumptions."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from citadel_agent.analytics.pricing import (
    ModelPricingEntry,
    PricingMethod,
    PricingStatus,
    UsageCost,
    allocate_percentage_costs,
    estimate_event_cost,
)
from citadel_agent.analytics.usage import UsageSourceKey, normalize_usage_document
from citadel_agent.portal.contracts import HubScope, TimeRange

TENANT_ID = "11111111-1111-4111-8111-111111111111"
SCOPE = HubScope(
    tenant_id=TENANT_ID,
    hub_id="citadel-production",
    environment="prod",
    subscription_id="22222222-2222-4222-8222-222222222222",
)
EVENT_TIME = datetime(2026, 7, 19, 11, tzinfo=UTC)


def _event(*, event_id: str = "llm-event-01", **overrides):
    document = {
        "id": event_id,
        "timestamp": "2026-07-19T11:00:00Z",
        "model": "gpt-model",
        "deploymentName": "chat",
        "gatewayRegion": "Sweden Central",
        "promptTokens": "1000",
        "responseTokens": "100",
        "promptCachedTokens": "200",
        "promptAudioTokens": "0",
        "completionAudioTokens": "0",
        "completionReasoningTokens": "0",
        **overrides,
    }
    source_key = UsageSourceKey.create(
        account="usage-account",
        database="usage-db",
        container="llm-usage-container",
        partition_key="partition-a",
        document_id=event_id,
    )
    return normalize_usage_document(document, source_key=source_key, scope=SCOPE)


def _price(**overrides) -> ModelPricingEntry:
    return ModelPricingEntry.model_validate(
        {
            "id": "pricing-gpt",
            "model": "gpt-model",
            "deploymentName": "chat",
            "isActive": True,
            "CostPerInputUnit": "2.00",
            "CostPerOutputUnit": "8.00",
            "CostPerCachedInputUnit": "0.50",
            "CostPerAudioInputUnit": "2.00",
            "CostPerCachedAudioInputUnit": "2.00",
            "CostPerAudioOutputUnit": "8.00",
            "CostPerReasoningOutputUnit": "8.00",
            "CostPerImageInputUnit": "2.00",
            "CostPerCachedImageInputUnit": "2.00",
            "CostUnit": "1000000",
            "BaseCost": "0",
            "Currency": "USD",
            "CalculationMethod": "tokens",
            "region": "ALL",
            "effectiveFrom": "2026-01-01T00:00:00Z",
            **overrides,
        }
    )


def test_effective_price_uses_decimal_cached_and_output_rates() -> None:
    result = estimate_event_cost(_event(), [_price()])

    assert result.status is PricingStatus.PRICED
    assert result.amount == Decimal("0.0025")
    assert result.currency == "USD"
    assert result.pricing_id == "pricing-gpt"
    assert result.effective_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_region_specific_rate_wins_and_inactive_historical_rate_still_applies() -> None:
    global_rate = _price(id="global", region="ALL")
    regional = _price(
        id="regional",
        isActive=False,
        region="Sweden Central",
        CostPerInputUnit="4",
        CostPerImageInputUnit="4",
        CostPerCachedImageInputUnit="4",
    )

    result = estimate_event_cost(_event(), [global_rate, regional])

    assert result.pricing_id == "regional"
    assert result.amount == Decimal("0.0041")


def test_undated_active_price_is_not_used_for_historical_event() -> None:
    undated = _price().model_copy(update={"effective_from": None})

    result = estimate_event_cost(_event(), [undated])

    assert result == UsageCost(
        status=PricingStatus.UNPRICED,
        warning_codes=["pricing-effective-date-missing"],
    )


def test_event_outside_effective_window_is_unpriced() -> None:
    ended = _price(effectiveTo="2026-07-19T11:00:00Z")

    result = estimate_event_cost(_event(), [ended])

    assert result.warning_codes == ["no-effective-price"]


def test_overlapping_effective_prices_are_not_silently_selected() -> None:
    first = _price(id="first")
    second = _price(id="second", CostPerInputUnit="3")

    result = estimate_event_cost(_event(), [first, second])

    assert result.warning_codes == ["ambiguous-effective-price"]


def test_missing_cached_breakdown_blocks_a_differential_cached_rate() -> None:
    event = _event()
    event = event.model_copy(
        update={
            "available_usage_fields": event.available_usage_fields - {"promptCachedTokens"},
            "usage": event.usage.model_copy(update={"cached_prompt": 0}),
        }
    )

    result = estimate_event_cost(event, [_price()])

    assert result.warning_codes == ["missing-promptCachedTokens"]


def test_overlapping_differential_audio_and_cached_components_are_unpriced() -> None:
    price = _price(CostPerAudioInputUnit="4", CostPerCachedAudioInputUnit="4")
    event = _event(promptAudioTokens="20")

    result = estimate_event_cost(event, [price])

    assert result.warning_codes == ["overlapping-token-components-ambiguous"]


def test_percentage_prices_wait_for_complete_period_allocation() -> None:
    fixed = _price(
        CalculationMethod="percentage",
        BaseCost="11000",
    )

    result = estimate_event_cost(_event(), [fixed])

    assert result.warning_codes == ["percentage-pricing-requires-complete-period-aggregation"]


def test_pricing_requires_timezone_aware_effective_dates() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _price(effectiveFrom="2026-01-01T00:00:00")


def test_percentage_method_is_a_typed_price_method() -> None:
    assert _price(CalculationMethod="percentage").calculation_method is PricingMethod.PERCENTAGE


def test_percentage_allocation_sums_exactly_to_base_cost() -> None:
    price = _percentage_price()
    first = _event(event_id="event-a", responseTokens="100")
    second = _event(event_id="event-b", responseTokens="200")
    third = _event(event_id="event-c", responseTokens="0")

    costs = allocate_percentage_costs(
        [first, second, third],
        price,
        window=_allocation_window(),
        complete_through=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert [cost.status for cost in costs] == [PricingStatus.PRICED] * 3
    assert [cost.amount for cost in costs] == [
        Decimal(11000) / Decimal(3),
        Decimal(22000) / Decimal(3),
        Decimal(0),
    ]
    assert sum(cost.amount for cost in costs if cost.amount is not None) == Decimal(11000)


def test_percentage_allocation_requires_complete_period() -> None:
    costs = allocate_percentage_costs(
        [_event()],
        _percentage_price(),
        window=_allocation_window(),
        complete_through=datetime(2026, 7, 31, 23, 59, tzinfo=UTC),
    )

    assert costs[0].warning_codes == ["percentage-period-incomplete"]


def test_percentage_allocation_requires_price_coverage_for_the_whole_window() -> None:
    costs = allocate_percentage_costs(
        [_event()],
        _percentage_price(effectiveFrom="2026-07-15T00:00:00Z"),
        window=_allocation_window(),
        complete_through=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert costs[0].warning_codes == ["percentage-pricing-window-not-covered"]


def test_percentage_allocation_requires_same_deployment() -> None:
    event = _event()
    event = event.model_copy(
        update={"dimensions": event.dimensions.model_copy(update={"deployment": "another-deployment"})}
    )

    costs = allocate_percentage_costs(
        [event],
        _percentage_price(),
        window=_allocation_window(),
        complete_through=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert costs[0].warning_codes == ["percentage-price-does-not-match-event"]


def test_percentage_allocation_requires_a_single_supported_weight_of_one() -> None:
    invalid_price = _percentage_price(CostPerInputUnit="1")
    unsupported_price = _percentage_price(
        CostPerOutputUnit="0",
        CostPerCachedAudioInputUnit="1",
    )

    invalid = allocate_percentage_costs(
        [_event()],
        invalid_price,
        window=_allocation_window(),
        complete_through=datetime(2026, 8, 1, tzinfo=UTC),
    )
    unsupported = allocate_percentage_costs(
        [_event()],
        unsupported_price,
        window=_allocation_window(),
        complete_through=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert invalid[0].warning_codes == ["percentage-weight-configuration-invalid"]
    assert unsupported[0].warning_codes == ["percentage-weight-unsupported"]


def test_percentage_allocation_does_not_treat_missing_weight_as_zero() -> None:
    event = _event()
    event = event.model_copy(
        update={
            "available_usage_fields": event.available_usage_fields - {"promptCachedTokens"},
            "usage": event.usage.model_copy(update={"cached_prompt": 0}),
        }
    )

    costs = allocate_percentage_costs(
        [event],
        _percentage_price(CostPerOutputUnit="0", CostPerCachedInputUnit="1"),
        window=_allocation_window(),
        complete_through=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert costs[0].warning_codes == ["percentage-weight-data-unavailable"]


def _percentage_price(**overrides) -> ModelPricingEntry:
    values = {
        "CalculationMethod": "percentage",
        "BaseCost": "11000",
        "CostPerInputUnit": "0",
        "CostPerOutputUnit": "1",
        "CostPerCachedInputUnit": "0",
        "CostPerAudioInputUnit": "0",
        "CostPerCachedAudioInputUnit": "0",
        "CostPerAudioOutputUnit": "0",
        "CostPerReasoningOutputUnit": "0",
        "CostPerImageInputUnit": "0",
        "CostPerCachedImageInputUnit": "0",
    }
    values.update(overrides)
    return _price(**values)


def _allocation_window() -> TimeRange:
    return TimeRange(
        start=datetime(2026, 7, 1, tzinfo=UTC),
        end=datetime(2026, 8, 1, tzinfo=UTC),
    )
