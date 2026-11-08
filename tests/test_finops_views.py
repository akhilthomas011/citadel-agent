"""Six analytical views reconcile one input with exact costs, UTC bins and privacy gates."""

import csv
import hashlib
import io
import json
import zipfile
from datetime import timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from azure.core.exceptions import HttpResponseError

from citadel_agent.analytics.materialize import AnalyticsMaterializer
from citadel_agent.analytics.rollups import UsageDimension
from citadel_agent.portal.authz import PortalGrantPermission
from citadel_agent.portal.contracts import TimeRange
from citadel_agent.portal.product_contracts import DashboardQuery, ReportStartRequest
from citadel_agent.portal.report_export import finops_csv
from citadel_agent.portal.reports import ReportUnavailable
from citadel_agent.portal.sessions import PortalStateConflict
from test_analytics_snapshots import Source
from test_analytics_snapshots import finish as publish
from test_finops_read_model import complete, service
from test_finops_reports import report_client
from test_portal_product import NOW, USER, usage_document
from test_portal_product import config as config_fixture
from test_portal_product import store as store_fixture

config = config_fixture
store = store_fixture


def test_pinned_reference_surface_contains_all_six_pages_and_forty_four_bound_visuals():
    path = (
        Path(__file__).resolve().parents[1]
        / "upstream"
        / "ai-hub-gateway"
        / "src"
        / "usage-reports"
        / "Citadel-Governance-Hub-Usage-Dashboard-V1.1-Incremental.pbix"
    )
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest()
        == "e853b6f11af8e519b736601cb5f50f98b8978c5613f0e92ffd1d2ad0f94603e0"
    )
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        counts = {}
        for name in names:
            if name.endswith("/page.json") and name.startswith("Report/definition/pages/"):
                page = json.loads(archive.read(name))
                prefix = name.removesuffix("page.json") + "visuals/"
                counts[page["displayName"]] = sum(
                    bool(json.loads(archive.read(value)).get("visual", {}).get("query", {}).get("queryState"))
                    for value in names
                    if value.startswith(prefix) and value.endswith("/visual.json")
                )
    assert counts == {
        "Utilization": 10,
        "Heatmap": 4,
        "Usage and Cost Overview": 8,
        "Model and Deployment Performance": 7,
        "Application and Gateway Usage": 7,
        "Peak Usage and Capacity Planning": 8,
    }


def controlled_source():
    midnight = (NOW - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    source = Source(0)
    rows = [
        dict(
            id="a",
            model="alpha",
            deploymentName="da",
            backendId="b1",
            productName="p1",
            appId="app1",
            promptTokens=100,
            responseTokens=50,
            totalTokens=150,
            timestamp=(midnight - timedelta(minutes=30)).strftime("%m/%d/%Y %I:%M:%S %p"),
        ),
        dict(
            id="b",
            model="alpha",
            deploymentName="da",
            backendId="b1",
            productName="p1",
            appId="app1",
            promptTokens=10,
            responseTokens=5,
            totalTokens=15,
            promptCachedTokens=5,
            timestamp=(midnight + timedelta(hours=1, minutes=30))
            .replace(tzinfo=timezone(timedelta(hours=2)))
            .isoformat(),
        ),
        dict(
            id="c",
            model="beta",
            deploymentName="db",
            backendId="b2",
            productName="p2",
            appId="app2",
            promptTokens=20,
            responseTokens=0,
            totalTokens=20,
            timestamp=(midnight + timedelta(minutes=15)).isoformat(),
        ),
        dict(
            id="d",
            model="unpriced",
            deploymentName="dx",
            backendId=None,
            productName="p1",
            appId="app1",
            promptTokens=1,
            responseTokens=2,
            totalTokens=3,
            timestamp=(midnight + timedelta(minutes=30)).isoformat(),
        ),
    ]
    source.documents = {
        row["id"]: usage_document(
            **row,
            customDimension1="private-user-fixture",
            customDimension2="private-session-fixture",
            operationName="chat",
            targetService="openai",
            aiGatewayId="managed",
            _ts=int((NOW - timedelta(seconds=10)).timestamp()),
            promptAudioTokens=0,
            completionAudioTokens=0,
            completionReasoningTokens=0,
            completionAcceptedPredictionTokens=1 if row["id"] == "a" else 0,
            completionRejectedPredictionTokens=0,
            promptImageTokens=0,
            promptCachedImageTokens=0,
        )
        for row in rows
    }
    source.prices = [
        dict(
            id=model,
            model=model,
            deploymentName=deployment,
            isActive=True,
            CostUnit=1000,
            CostPerInputUnit=1,
            CostPerCachedInputUnit=1,
            CostPerOutputUnit=2,
            Currency=currency,
            CalculationMethod="tokens",
            effective_from=(midnight - timedelta(days=5)).isoformat(),
        )
        for model, deployment, currency in [("alpha", "da", "USD"), ("beta", "db", "EUR")]
    ]
    return source, DashboardQuery(
        window=TimeRange(start=midnight - timedelta(hours=1), end=midnight + timedelta(hours=1))
    )


@pytest.mark.parametrize("input_kind", ["source-scan", "read-model"])
def test_every_projection_reconciles_currency_tokens_counts_and_utc_across_midnight(
    config, store, input_kind
):
    source, query = controlled_source()
    reports, _, _, _ = service(config, store, source)
    request = ReportStartRequest(
        request_id=uuid4(), query=query, input_kind=input_kind, include_analysis=True
    )
    result = complete(reports, reports.start(request, USER).id).result
    assert result.requests == 4
    assert result.usage.prompt == 131 and result.usage.completion == 57
    assert result.usage.total == 188
    assert result.usage.cached_prompt == 5
    assert {cost.currency: cost.amount for cost in result.costs} == {
        "USD": Decimal("0.22"),
        "EUR": Decimal("0.02"),
    }
    analysis = result.analysis
    assert analysis is not None
    assert len(analysis.profiles) == 17
    for name, rows in analysis.profiles.items():
        assert sum(row.requests for row in rows) == 4, name
        assert sum(row.usage.prompt for row in rows) == 131, name
        assert sum(row.usage.completion for row in rows) == 57, name
        assert sum(row.usage.total for row in rows) == 188, name
        assert sum(row.unpriced_requests for row in rows) == 1, name
        assert sum(cost.amount for row in rows for cost in row.costs if cost.currency == "USD") == Decimal(
            "0.22"
        )
        assert sum(cost.amount for row in rows for cost in row.costs if cost.currency == "EUR") == Decimal(
            "0.02"
        )
    product = {row.dimensions[UsageDimension.PRODUCT]: row for row in analysis.profiles["product"]}
    assert product["p1"].requests == 3 and product["p1"].usage.total == 168
    assert product["p2"].requests == 1 and product["p2"].usage.total == 20
    assert {row.hour_utc: row.usage.total for row in analysis.hour_of_day} == {23: 165, 0: 23}
    serialized = result.model_dump_json()
    assert "private-user-fixture" not in serialized and "private-session-fixture" not in serialized
    rows = list(csv.DictReader(io.StringIO(finops_csv(query, result)["content"])))
    for name in analysis.profiles:
        assert sum(
            Decimal(row["estimated_cost"])
            for row in rows
            if row["profile"] == name and row["currency"] == "USD"
        ) == Decimal("0.22")
    assert {row["report_id"] for row in rows} == {result.report_id}
    assert {row["input_kind"] for row in rows} == {input_kind}


def test_shared_filters_include_unattributed_and_preserve_other_dimensions(config, store):
    source, query = controlled_source()
    reports, _, _, _ = service(config, store, source)
    missing = query.model_copy(update={"dimension_filters": {UsageDimension.BACKEND: None}})
    result = complete(
        reports,
        reports.start(ReportStartRequest(request_id=uuid4(), query=missing, include_analysis=True), USER).id,
    ).result
    assert result.requests == 1
    assert result.usage.total == 3
    combined = query.model_copy(
        update={
            "dimension_filters": {
                UsageDimension.MODEL: "alpha",
                UsageDimension.PRODUCT: "p1",
                UsageDimension.APPLICATION: "app1",
            }
        }
    )
    result = complete(
        reports,
        reports.start(ReportStartRequest(request_id=uuid4(), query=combined, include_analysis=True), USER).id,
    ).result
    assert result.requests == 2 and result.usage.total == 165
    assert result.analysis.profiles["model_backend"][0].dimensions == {
        UsageDimension.MODEL: "alpha",
        UsageDimension.BACKEND: "b1",
    }


def test_missing_tokens_are_explicit_per_row_and_csv_not_reconstructed(config, store):
    source, query = controlled_source()
    del source.documents["a"]["totalTokens"]
    del source.documents["a"]["completionReasoningTokens"]
    reports, _, _, _ = service(config, store, source)
    result = complete(
        reports,
        reports.start(ReportStartRequest(request_id=uuid4(), query=query, include_analysis=True), USER).id,
    ).result
    assert result.usage.total is None
    alpha = next(
        row for row in result.analysis.profiles["model"] if row.dimensions[UsageDimension.MODEL] == "alpha"
    )
    assert alpha.usage.total is None
    assert {"totalTokens", "completionReasoningTokens"} <= set(alpha.unavailable_tokens)
    rows = list(csv.DictReader(io.StringIO(finops_csv(query, result)["content"])))
    alpha_csv = next(row for row in rows if row["profile"] == "model" and row["model"] == "alpha")
    assert alpha_csv["total"] == "" and alpha_csv["completion_reasoning"] == ""
    assert json.loads(alpha_csv["unavailable_token_fields"])


def test_custom_attribution_requires_configuration_separate_grant_and_current_export_permission(
    config, store
):
    source, query = controlled_source()
    config = config.model_copy(update={"custom_dimension_labels": {"custom_dimension_1": "User cohort"}})
    _reports, reader, _, _ = service(config, store, source)
    plain_grants = frozenset({PortalGrantPermission.FINOPS_DATA, PortalGrantPermission.EXPORTS})
    client = report_client(config, store, source, analytics=reader, permissions=plain_grants)
    body = {
        "requestId": str(uuid4()),
        "query": query.to_param(),
        "includeAnalysis": True,
        "includeCustomDimensions": True,
    }
    assert client.post("/internal/finops/reports", json=body).status_code == 403
    allowed = report_client(config, store, source, analytics=reader)
    accepted = allowed.post("/internal/finops/reports", json=body)
    assert accepted.status_code == 202
    path = "/internal/finops/reports/" + accepted.json()["id"]
    finished = allowed.post(path + "/advance").json()
    assert finished["status"] == "succeeded"
    assert finished["result"]["analysis"]["customDimensions"] == ["custom_dimension_1"]
    assert "private-user-fixture" in json.dumps(finished)
    assert "private-session-fixture" not in json.dumps(finished)
    for method, suffix in [("GET", ""), ("POST", "/advance"), ("POST", "/cancel"), ("POST", "/export")]:
        assert client.request(method, path + suffix).status_code == 403
    assert client.get("/internal/finops/reports").json() == []
    assert "private-user-fixture" in allowed.post(path + "/export").json()["content"]


def test_checkpointed_analysis_retry_and_request_identity_do_not_replay_usage(config, store, monkeypatch):
    source = Source(600)
    query = DashboardQuery(window=TimeRange(start=NOW - timedelta(days=1), end=NOW))
    reports, _, _, _ = service(config, store, source)
    request = ReportStartRequest(
        request_id=uuid4(), query=query, input_kind="source-scan", include_analysis=True
    )
    run = reports.start(request, USER)
    with pytest.raises(PortalStateConflict):
        reports.start(request.model_copy(update={"include_analysis": False}), USER)
    page = source.query_page
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HttpResponseError(message="Synthetic interrupted page.")
        return page(*args, **kwargs)

    monkeypatch.setattr(source, "query_page", fail_second)
    with pytest.raises(ReportUnavailable):
        reports.advance(run.id, USER)
    assert reports.get(run.id, USER).matched_requests == 500
    result = complete(reports, run.id).result
    assert result.requests == 600
    assert all(sum(row.requests for row in rows) == 600 for rows in result.analysis.profiles.values())


def test_decoded_reference_simple_token_cost_and_total_match_for_covered_single_currency_inputs(
    config, store
):
    source, query = controlled_source()
    del source.documents["d"]
    source.prices[1]["Currency"] = "USD"
    reports, _, _, _ = service(config, store, source)
    result = complete(
        reports,
        reports.start(ReportStartRequest(request_id=uuid4(), query=query, include_analysis=True), USER).id,
    ).result
    prices = {price["deploymentName"]: price for price in source.prices}
    # CostUSDX is the two-rate calculated column used by the pinned PBIR visuals.
    reference_cost = sum(
        (
            Decimal(prices[row["deploymentName"]]["CostPerInputUnit"]) * Decimal(row["promptTokens"])
            + Decimal(prices[row["deploymentName"]]["CostPerOutputUnit"]) * Decimal(row["responseTokens"])
        )
        / Decimal(prices[row["deploymentName"]]["CostUnit"])
        for row in source.documents.values()
    )
    assert reference_cost == Decimal("0.24") == result.costs[0].amount
    assert sum(int(row["totalTokens"]) for row in source.documents.values()) == 185 == result.usage.total


def test_intentional_reference_differences_keep_product_filters_and_cached_rates_effective(config, store):
    source, query = controlled_source()
    source.prices[0]["CostPerCachedInputUnit"] = "0.5"
    query = query.model_copy(update={"dimension_filters": {UsageDimension.PRODUCT: "p1"}})
    reports, _, _, _ = service(config, store, source)
    result = complete(
        reports,
        reports.start(ReportStartRequest(request_id=uuid4(), query=query, include_analysis=True), USER).id,
    ).result
    assert result.usage.total == 168
    # The decoded GrandTotalTokens uses ALL(productName), intentionally not copied into the portal.
    reference_grand_total = sum(int(row["totalTokens"]) for row in source.documents.values())
    assert reference_grand_total == 188
    assert result.costs[0].amount == Decimal("0.2175")
    assert Decimal("0.22") != result.costs[0].amount


def test_exact_analytical_bucket_guard_fails_without_partial_output_or_export(config, store, monkeypatch):
    source, query = controlled_source()
    reports, _, _, _ = service(config, store, source)

    def start():
        return reports.start(ReportStartRequest(request_id=uuid4(), query=query, include_analysis=True), USER)

    baseline = complete(reports, start().id).result
    count = sum(len(rows) for rows in baseline.analysis.profiles.values())
    monkeypatch.setattr("citadel_agent.portal.reports.MAX_ANALYSIS_BUCKETS", count)
    assert complete(reports, start().id).result.requests == 4
    monkeypatch.setattr("citadel_agent.portal.reports.MAX_ANALYSIS_BUCKETS", count - 1)
    run = start()
    failed = reports.advance(run.id, USER)
    assert failed.status == "failed" and failed.error_code == "report-cardinality-limit"
    assert failed.result is None
    with pytest.raises(PortalStateConflict):
        reports.completed(run.id, USER)


def test_custom_labels_are_disabled_by_default_and_invalid_request_flags_are_rejected(config, store):
    source, query = controlled_source()
    client = report_client(config, store, source)
    assert client.get("/internal/portal").json()["customDimensionLabels"] == {}
    body = {
        "requestId": str(uuid4()),
        "query": query.to_param(),
        "includeAnalysis": True,
        "includeCustomDimensions": True,
    }
    assert client.post("/internal/finops/reports", json=body).status_code == 400
    body["includeAnalysis"] = False
    assert client.post("/internal/finops/reports", json=body).status_code == 422
    body["includeCustomDimensions"] = False
    body["query"]["dimensionFilters"] = {"backend": "   "}
    assert client.post("/internal/finops/reports", json=body).status_code == 422


def test_filter_drills_pin_the_original_generation_until_explicit_refresh(config, store):
    source, query = controlled_source()
    reports, reader, first, clock = service(config, store, source)
    clock.now += timedelta(minutes=6)
    source.documents["a"].update(promptTokens=200, totalTokens=250, _ts=int(clock.now.timestamp()))
    later = publish(AnalyticsMaterializer(reader.store, reports.data, clock=clock), full=True)
    assert later.generation != first.generation
    selected = query.model_copy(update={"dimension_filters": {UsageDimension.PRODUCT: "p1"}})
    pinned = ReportStartRequest(
        request_id=uuid4(),
        query=selected,
        include_analysis=True,
        read_model_version=first.generation,
    )
    result = complete(reports, reports.start(pinned, USER).id).result
    assert result.read_model_version == first.generation and result.usage.total == 168
    with pytest.raises(PortalStateConflict):
        reports.start(pinned.model_copy(update={"read_model_version": later.generation}), USER)
    refreshed = complete(
        reports,
        reports.start(ReportStartRequest(request_id=uuid4(), query=selected, include_analysis=True), USER).id,
    ).result
    assert refreshed.read_model_version == later.generation and refreshed.usage.total == 268
