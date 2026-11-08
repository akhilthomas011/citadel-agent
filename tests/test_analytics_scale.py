"""Controlled 100k-plus usage reconciles exact token and complete-period monetary totals."""

import csv
import io
from datetime import timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

from citadel_agent.analytics.materialize import AnalyticsMaterializer
from citadel_agent.analytics.pricing import ModelPricingEntry
from citadel_agent.analytics.read_model import AnalyticsReader
from citadel_agent.analytics.rollups import UsageDimension
from citadel_agent.analytics.snapshots import (
    EventRecord,
    PeriodCompletenessProof,
    add_digest,
    pricing_fingerprint,
    source_fingerprint,
)
from citadel_agent.analytics.usage import UsageObservation
from citadel_agent.portal.contracts import TimeRange
from citadel_agent.portal.product_contracts import DashboardQuery, ReportStartRequest
from citadel_agent.portal.report_export import finops_csv
from citadel_agent.portal.reports import FinOpsReportService
from citadel_agent.portal.state import ProductStore
from test_analytics_snapshots import Source, amounts, finish, records, setup
from test_portal_product import NOW, USER
from test_portal_product import Container as PortalContainer
from test_portal_product import config as config_fixture

config = config_fixture


def test_over_one_hundred_thousand_events_reconcile_two_fixed_periods_and_token_prices(config):
    count = 100_001
    source = Source(count)
    periods = [
        TimeRange(start=NOW - timedelta(days=4), end=NOW - timedelta(days=2)),
        TimeRange(start=NOW - timedelta(days=2), end=NOW - timedelta(minutes=1)),
    ]
    for index, document in enumerate(source.documents.values()):
        document.update(
            appId=f"application-{index % 7}",
            productName=f"product-{index % 4}",
            gatewayRegion="eastus" if index % 2 else "westus",
            promptCachedTokens=0,
            promptAudioTokens=0,
            promptImageTokens=0,
            promptCachedImageTokens=0,
            completionAudioTokens=0,
            completionReasoningTokens=0,
            totalTokens=150,
        )
        if index % 3:
            period = periods[index % 2]
            document.update(
                model="ptu-model",
                deploymentName="ptu-deployment",
                timestamp=(period.start + timedelta(hours=2 + index % 24)).isoformat(),
            )
    prices = [
        ModelPricingEntry(
            id="reviewed-token",
            model="gpt-test",
            deploymentName="test-deployment",
            isActive=True,
            CostUnit=1000,
            CostPerInputUnit=2,
            CostPerOutputUnit=4,
            Currency="USD",
            CalculationMethod="tokens",
            effective_from=NOW - timedelta(days=90),
        ),
        *[
            ModelPricingEntry(
                id=f"reviewed-fixed-{index}",
                model="ptu-model",
                deploymentName="ptu-deployment",
                isActive=True,
                CostUnit=1,
                CostPerInputUnit=1,
                Currency="USD",
                BaseCost=str(1000 + index * 500),
                CalculationMethod="percentage",
                effective_from=period.start,
                effective_to=period.end,
                AccountingPeriod=period,
            )
            for index, period in enumerate(periods)
        ],
    ]
    source.prices = [price.model_dump(by_alias=True, mode="json") for price in prices]
    connected = config.model_copy(
        update={
            "completeness_container": "trusted-completeness",
            "custom_dimension_labels": {
                "custom_dimension_1": "Cost centre",
                "custom_dimension_2": "Department",
            },
        }
    )
    store, data, worker, clock = setup(connected, source)
    digests = ["0" * 64, "0" * 64]
    quantities = [0, 0]
    for document in source.documents.values():
        event = data.normalize(document)
        observation = UsageObservation.model_validate(event.model_dump(exclude={"source_key"}))
        for index, period in enumerate(periods):
            if period.start <= event.event_time < period.end:
                quantities[index] += 1
                digests[index] = add_digest(digests[index], observation)
    source.proofs = [
        PeriodCompletenessProof(
            scope=config.scope,
            source_fingerprint=source_fingerprint(connected),
            pricing_fingerprint=pricing_fingerprint(prices),
            window=period,
            complete_through=period.end,
            issued_at=NOW,
            source_write_cutoff=int(NOW.timestamp()),
            event_count=quantities[index],
            event_digest=digests[index],
        ).model_dump(by_alias=True, mode="json")
        for index, period in enumerate(periods)
    ]
    info = finish(worker)
    assert info.event_count == count
    token_requests = (count + 2) // 3
    expected = Decimal(token_requests) * Decimal("0.4") + Decimal(2500)
    with localcontext() as context:
        context.prec = 100
        assert amounts(store, info, "hour") == {"USD": expected}
        assert amounts(store, info, "day") == {"USD": expected}
        events = [store.payload(store.decode(value), EventRecord) for value in records(store, info, "event")]
        assert sum(event.cost.amount for event in events) == expected
        assert sum(event.observation.usage.prompt for event in events) == count * 100
        assert sum(event.observation.usage.completion for event in events) == count * 50
        del events
        reports = FinOpsReportService(
            ProductStore(PortalContainer(), config.scope),
            data,
            analytics=AnalyticsReader(store, source_fingerprint(connected), clock=clock),
        )
        source_calls = source.calls
        for dimension in UsageDimension:
            query = DashboardQuery(
                window=TimeRange(start=periods[0].start, end=periods[1].end), group_by=dimension
            )
            run = reports.start(ReportStartRequest(request_id=uuid4(), query=query), USER, custom_access=True)
            for _ in range(100):
                run = reports.advance(run.id, USER)
                if run.status in {"succeeded", "failed"}:
                    break
            assert run.status == "succeeded", (dimension, run.error_code)
            result = run.result
            assert result.requests == count
            assert result.usage.prompt == count * 100
            assert result.unpriced_requests == 0
            assert result.costs[0].amount == expected
            rows = list(csv.DictReader(io.StringIO(finops_csv(query, result)["content"])))
            assert sum(Decimal(row["estimated_cost"]) for row in rows) == expected
            assert {row["read_model_version"] for row in rows} == {info.generation}
            assert sum(int(row["prompt_tokens"]) for row in rows) == count * 100
        assert source.calls == source_calls
    assert AnalyticsMaterializer(store, data, clock=clock).step() == "idle"
