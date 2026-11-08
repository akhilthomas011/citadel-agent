# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for deterministic daily FinOps aggregates."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from citadel_agent.analytics.pricing import PricingStatus, UsageCost
from citadel_agent.analytics.rollups import (
    FinOpsCheckpoint,
    IncrementalFinOps,
    UsageDimension,
    aggregate_finops_events,
)
from citadel_agent.analytics.usage import (
    NormalizedUsageEvent,
    UsageSourceKey,
    normalize_usage_document,
)
from citadel_agent.portal.contracts import (
    DataQuality,
    DataSource,
    HubScope,
    ObservationState,
    TimeRange,
)

SCOPE = HubScope(
    tenant_id="11111111-1111-4111-8111-111111111111",
    hub_id="citadel-production",
    environment="prod",
    subscription_id="22222222-2222-4222-8222-222222222222",
)
WINDOW = TimeRange(
    start=datetime(2026, 7, 19, tzinfo=UTC),
    end=datetime(2026, 7, 21, tzinfo=UTC),
)
OBSERVED_AT = datetime(2026, 7, 21, 1, tzinfo=UTC)
EFFECTIVE_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _event(
    event_id: str,
    *,
    timestamp: str = "2026-07-19T12:00:00Z",
    prompt_tokens: str = "100",
    response_tokens: str = "100",
    total_tokens: str | None = "200",
    product: str = "Payments",
) -> NormalizedUsageEvent:
    document = {
        "id": event_id,
        "timestamp": timestamp,
        "model": "gpt-model",
        "deploymentName": "chat",
        "backendId": "backend-1",
        "appId": "app-1",
        "productName": product,
        "gatewayRegion": "East US",
        "promptTokens": prompt_tokens,
        "responseTokens": response_tokens,
        "promptCachedTokens": "0",
        "completionReasoningTokens": "0",
    }
    if total_tokens is not None:
        document["totalTokens"] = total_tokens
    source_key = UsageSourceKey.create(
        account="usage-account",
        database="usage-db",
        container="llm-usage-container",
        partition_key="partition-a",
        document_id=event_id,
    )
    return normalize_usage_document(document, source_key=source_key, scope=SCOPE)


def _quality(window: TimeRange = WINDOW) -> DataQuality:
    return DataQuality(
        source=DataSource.COSMOS_USAGE,
        observed_at=OBSERVED_AT,
        event_window=window,
        state=ObservationState.HEALTHY,
        watermark=window.end,
    )


def _cost(amount: str | None, *, currency: str = "USD", warning: str = "") -> UsageCost:
    if amount is None:
        return UsageCost(
            status=PricingStatus.UNPRICED,
            warning_codes=[warning] if warning else ["no-effective-price"],
        )
    return UsageCost(
        status=PricingStatus.PRICED,
        amount=Decimal(amount),
        currency=currency,
        pricing_id="price-1",
        effective_at=EFFECTIVE_AT,
    )


def test_common_daily_rollup_preserves_full_precision_and_pricing_coverage() -> None:
    first = _event("event-1")
    second = _event("event-2")
    next_day = _event("event-3", timestamp="2026-07-20T01:00:00Z")
    costs = {
        first.event_id: _cost("0.00000000000000000001"),
        second.event_id: _cost(None, warning="missing-price"),
        next_day.event_id: _cost("2.25", currency="EUR"),
    }

    result = aggregate_finops_events([first, second, next_day], costs, window=WINDOW, quality=_quality())

    assert len(result) == 2
    first_day, second_day = result
    assert first_day.window == TimeRange(
        start=datetime(2026, 7, 19, tzinfo=UTC),
        end=datetime(2026, 7, 20, tzinfo=UTC),
    )
    assert first_day.dimensions.model == "gpt-model"
    assert first_day.dimensions.product == "Payments"
    assert first_day.usage.prompt == 200
    assert first_day.usage.completion == 200
    assert first_day.usage.total == 400
    assert first_day.costs[0].amount == Decimal("0.00000000000000000001")
    assert first_day.pricing_coverage.priced_usage_percent == Decimal(50)
    assert first_day.pricing_coverage.unpriced_dimensions
    assert first_day.pricing_coverage.warning_codes == ["missing-price"]
    assert first_day.quality.state is ObservationState.PARTIAL
    assert "usage-field-unavailable:promptImageTokens" in first_day.quality.warning_codes
    assert second_day.costs[0].currency == "EUR"
    assert second_day.costs[0].amount == Decimal("2.25")


def test_total_only_rollup_clips_daily_windows_to_the_requested_interval() -> None:
    window = TimeRange(
        start=datetime(2026, 7, 19, 12, tzinfo=UTC),
        end=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )
    first = _event("event-1", timestamp="2026-07-19T13:00:00Z")
    second = _event("event-2", timestamp="2026-07-20T01:00:00Z")

    result = aggregate_finops_events(
        [second, first],
        {first.event_id: _cost("1"), second.event_id: _cost("2")},
        window=window,
        quality=_quality(window),
        group_by=(),
    )

    assert [item.window for item in result] == [
        TimeRange(
            start=datetime(2026, 7, 19, 12, tzinfo=UTC),
            end=datetime(2026, 7, 20, tzinfo=UTC),
        ),
        TimeRange(
            start=datetime(2026, 7, 20, tzinfo=UTC),
            end=datetime(2026, 7, 20, 12, tzinfo=UTC),
        ),
    ]
    assert result[0].dimensions.model is None
    assert result[0].costs[0].amount == Decimal("1")
    assert result[1].costs[0].amount == Decimal("2")


def test_rollup_preserves_currency_and_total_token_completeness() -> None:
    missing_total = _event("event-1", total_tokens=None)
    known_total = _event("event-2")

    result = aggregate_finops_events(
        [missing_total, known_total],
        {
            missing_total.event_id: _cost("1", currency="EUR"),
            known_total.event_id: _cost("2", currency="USD"),
        },
        window=WINDOW,
        quality=_quality(),
        group_by=(),
    )

    assert len(result) == 1
    assert result[0].usage.total is None
    assert [amount.currency for amount in result[0].costs] == ["EUR", "USD"]
    assert "usage-field-unavailable:totalTokens" in result[0].quality.warning_codes


def test_rollup_reports_zero_token_coverage_explicitly() -> None:
    event = _event("event-1", prompt_tokens="0", response_tokens="0", total_tokens="0")

    result = aggregate_finops_events(
        [event],
        {event.event_id: _cost("0")},
        window=WINDOW,
        quality=_quality(),
        group_by=(),
    )

    assert result[0].pricing_coverage.priced_usage_percent == Decimal(0)
    assert result[0].pricing_coverage.warning_codes == ["pricing-coverage-zero-token-volume"]


def test_rollup_adds_decimal_amounts_without_context_rounding() -> None:
    first = _event("event-1")
    second = _event("event-2")
    amount = "9.999999999999999999999999999"

    result = aggregate_finops_events(
        [first, second],
        {first.event_id: _cost(amount), second.event_id: _cost(amount)},
        window=WINDOW,
        quality=_quality(),
        group_by=(),
    )

    assert result[0].costs[0].amount == Decimal("19.999999999999999999999999998")


def test_rollups_are_deterministic_independent_of_event_order() -> None:
    events = [_event("event-a"), _event("event-b", product="Search")]
    costs = {event.event_id: _cost(str(index + 1)) for index, event in enumerate(events)}

    forward = aggregate_finops_events(events, costs, window=WINDOW, quality=_quality())
    reverse = aggregate_finops_events(list(reversed(events)), costs, window=WINDOW, quality=_quality())

    assert forward == reverse
    assert [item.dimensions.product for item in forward] == ["Payments", "Search"]


def test_json_checkpoint_roundtrip_matches_single_pass_without_rounding_or_lost_metadata():
    events = [
        _event("first", total_tokens=None),
        _event("second"),
        _event("third", timestamp="2026-07-20T01:00:00Z"),
    ]
    costs = {
        events[0].event_id: _cost("9.999999999999999999999999999"),
        events[1].event_id: _cost(None),
        events[2].event_id: _cost("2.25", currency="EUR"),
    }
    accumulator = IncrementalFinOps(SCOPE, WINDOW)
    accumulator.add(events[:1], {events[0].event_id: costs[events[0].event_id]})
    snapshot = FinOpsCheckpoint.model_validate_json(accumulator.checkpoint().model_dump_json())
    resumed = IncrementalFinOps.restore(snapshot)
    resumed.add(events[1:], {event.event_id: costs[event.event_id] for event in events[1:]})
    assert resumed.results(_quality()) == aggregate_finops_events(
        events, costs, window=WINDOW, quality=_quality()
    )
    assert resumed.usage_weight == 600
    assert resumed.priced_weight == 400
    assert accumulator.usage_weight == 200


def test_invalid_checkpoint_cannot_change_report_window_or_pricing_weights():
    event = _event("first")
    accumulator = IncrementalFinOps(SCOPE, WINDOW)
    accumulator.add([event], {event.event_id: _cost("1")})
    checkpoint = accumulator.checkpoint().model_dump(mode="json")
    checkpoint["buckets"][0]["total_usage_weight"] += 1
    with pytest.raises(ValueError, match="match observed"):
        FinOpsCheckpoint.model_validate(checkpoint)
    checkpoint = accumulator.checkpoint().model_dump(mode="json")
    checkpoint["buckets"][0]["window"]["end"] = "2026-07-19T13:00:00Z"
    with pytest.raises(ValueError, match="UTC-day boundaries"):
        FinOpsCheckpoint.model_validate(checkpoint)


def test_rollup_rejects_costs_that_do_not_match_the_event_set() -> None:
    with pytest.raises(ValueError, match="exactly"):
        aggregate_finops_events(
            [_event("event-1")],
            {},
            window=WINDOW,
            quality=_quality(),
        )


def test_rollup_rejects_duplicate_group_dimensions() -> None:
    event = _event("event-1")

    with pytest.raises(ValueError, match="unique"):
        aggregate_finops_events(
            [event],
            {event.event_id: _cost("1")},
            window=WINDOW,
            quality=_quality(),
            group_by=(UsageDimension.PRODUCT, UsageDimension.PRODUCT),
        )
