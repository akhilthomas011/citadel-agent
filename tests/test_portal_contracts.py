"""Portal API contracts preserve scope, provenance and financial distinctions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from citadel_agent.portal.contracts import (
    CostBasis,
    CountValue,
    DataQuality,
    DataSource,
    FinOpsAggregate,
    HubScope,
    MonetaryAmount,
    ObservationState,
    OperationalMetric,
    PortalRole,
    PricingCoverage,
    TimeRange,
    TokenUsage,
    UsageDimensions,
)

TENANT = "11111111-1111-4111-8111-111111111111"
SUBSCRIPTION = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def _scope() -> HubScope:
    return HubScope(
        tenant_id=TENANT,
        hub_id="production-hub",
        environment="prod",
        subscription_id=SUBSCRIPTION,
    )


def _quality() -> DataQuality:
    return DataQuality(
        source=DataSource.COSMOS_USAGE,
        observed_at=NOW,
        event_window=TimeRange(start=NOW - timedelta(days=1), end=NOW),
        state=ObservationState.HEALTHY,
        watermark=NOW,
    )


def test_roles_are_explicit_and_not_request_selected() -> None:
    assert {role.value for role in PortalRole} == {
        "Platform.Viewer",
        "Platform.FinOpsAnalyst",
        "Platform.Operator",
        "Platform.Approver",
    }


def test_time_ranges_normalize_offsets_to_utc() -> None:
    value = TimeRange(
        start=datetime.fromisoformat("2026-10-15T12:00:00+02:00"),
        end=datetime.fromisoformat("2026-10-15T13:00:00+02:00"),
    )
    assert value.start == datetime(2026, 10, 15, 10, tzinfo=UTC)
    assert value.end == datetime(2026, 10, 15, 11, tzinfo=UTC)


@pytest.mark.parametrize(
    "start,end",
    [
        (datetime(2026, 10, 15, 12), datetime(2026, 10, 15, 13)),
        (NOW, NOW),
        (NOW + timedelta(minutes=1), NOW),
    ],
)
def test_time_ranges_reject_naive_or_invalid_boundaries(start: datetime, end: datetime) -> None:
    with pytest.raises(ValidationError):
        TimeRange(start=start, end=end)


def test_unknown_count_is_not_zero() -> None:
    unknown = CountValue(
        value=None,
        state=ObservationState.UNKNOWN,
        source=DataSource.APIM,
        observed_at=NOW,
    )
    zero = CountValue(
        value=0,
        state=ObservationState.HEALTHY,
        source=DataSource.APIM,
        observed_at=NOW,
    )
    assert unknown.value is None
    assert zero.value == 0
    with pytest.raises(ValidationError):
        CountValue(
            value=0,
            state=ObservationState.UNKNOWN,
            source=DataSource.APIM,
            observed_at=NOW,
        )


def test_finops_aggregate_keeps_actual_and_usage_estimate_separate() -> None:
    report = FinOpsAggregate(
        scope=_scope(),
        window=TimeRange(start=NOW - timedelta(days=1), end=NOW),
        dimensions=UsageDimensions(provider="Azure OpenAI", model="model-a"),
        usage=TokenUsage(prompt=100, completion=25, cached_prompt=20, total=125),
        costs=[
            MonetaryAmount(
                amount=Decimal("0.125"),
                currency="USD",
                basis=CostBasis.USAGE_ESTIMATE,
                source=DataSource.COSMOS_USAGE,
                effective_at=NOW,
            ),
            MonetaryAmount(
                amount=Decimal("0.14"),
                currency="USD",
                basis=CostBasis.ACTUAL,
                source=DataSource.COST_MANAGEMENT,
                effective_at=NOW,
            ),
        ],
        pricing_coverage=PricingCoverage(
            priced_usage_percent=Decimal("80"),
            unpriced_dimensions=["model-b"],
        ),
        quality=_quality(),
    )
    payload = report.model_dump(by_alias=True, mode="json")
    assert payload["costs"][0]["basis"] == "usage-estimate"
    assert payload["costs"][1]["basis"] == "actual"
    assert payload["pricingCoverage"]["pricedUsagePercent"] == "80"
    assert payload["scope"]["environment"] == "prod"


def test_finops_rejects_duplicate_basis_currency_buckets() -> None:
    cost = MonetaryAmount(
        amount=Decimal("1"),
        currency="USD",
        basis=CostBasis.ACTUAL,
        source=DataSource.COST_MANAGEMENT,
    )
    with pytest.raises(ValidationError):
        FinOpsAggregate(
            scope=_scope(),
            window=TimeRange(start=NOW - timedelta(days=1), end=NOW),
            dimensions=UsageDimensions(),
            usage=TokenUsage(),
            costs=[cost, cost],
            pricing_coverage=PricingCoverage(priced_usage_percent=Decimal("100")),
            quality=_quality(),
        )


def test_quality_rejects_naive_timestamps_and_deduplicates_warnings() -> None:
    quality = DataQuality(
        source=DataSource.LOG_ANALYTICS,
        observed_at=NOW,
        state=ObservationState.PARTIAL,
        warning_codes=["timeout", "timeout", "", " partial "],
    )
    assert quality.warning_codes == ["timeout", "partial"]
    with pytest.raises(ValidationError):
        DataQuality(
            source=DataSource.LOG_ANALYTICS,
            observed_at=datetime(2026, 10, 15, 12),
            state=ObservationState.UNKNOWN,
        )


def test_unavailable_operational_metric_does_not_claim_zero() -> None:
    metric = OperationalMetric(
        name="request.p95",
        value=None,
        unit="ms",
        state=ObservationState.UNAVAILABLE,
        quality=DataQuality(
            source=DataSource.APPLICATION_INSIGHTS,
            observed_at=NOW,
            state=ObservationState.UNAVAILABLE,
            warning_codes=["no-data"],
        ),
    )
    assert metric.value is None
    with pytest.raises(ValidationError):
        OperationalMetric(
            name="request.p95",
            value=Decimal("0"),
            unit="ms",
            state=ObservationState.UNKNOWN,
            quality=metric.quality,
        )


def test_count_and_amount_fields_reject_negative_values() -> None:
    with pytest.raises(ValidationError):
        CountValue(value=-1, state="healthy", source="apim", observed_at=NOW)
    with pytest.raises(ValidationError):
        MonetaryAmount(amount=Decimal("-0.01"), currency="USD", basis="actual", source="cost-management")
