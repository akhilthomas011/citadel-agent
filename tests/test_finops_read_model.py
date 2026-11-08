"""Authorized reports pin one maintained generation and never prorate partial-hour costs."""

from datetime import timedelta
from uuid import uuid4

import pytest
from azure.core.exceptions import HttpResponseError

from citadel_agent.analytics.read_model import AnalyticsReader
from citadel_agent.analytics.snapshots import source_fingerprint
from citadel_agent.portal.contracts import PortalRole, TimeRange
from citadel_agent.portal.product_contracts import DashboardQuery, ReportStartRequest
from citadel_agent.portal.report_export import finops_csv
from citadel_agent.portal.reports import FinOpsReportService, ReportUnavailable
from test_analytics_snapshots import Source, setup
from test_analytics_snapshots import finish as publish
from test_finops_reports import report_client
from test_portal_product import NOW, USER, identity
from test_portal_product import config as config_fixture
from test_portal_product import store as store_fixture

config = config_fixture
store = store_fixture


def service(config, store, source):
    analytics, data, worker, clock = setup(config, source)
    info = publish(worker)
    reader = AnalyticsReader(analytics, source_fingerprint(config), clock=clock)
    return FinOpsReportService(store, data, analytics=reader), reader, info, clock


def complete(reports, report_id):
    for _ in range(100):
        run = reports.advance(report_id, USER)
        if run.status in {"succeeded", "failed"}:
            assert run.status == "succeeded", run.error_code
            return run
    pytest.fail("A finite maintained report did not complete.")


def test_partial_hours_use_exact_edges_and_only_complete_hourly_buckets(config, store):
    source = Source(4)
    start = (NOW - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    for document, minutes in zip(source.documents.values(), [30, 70, 110, 130], strict=True):
        document["timestamp"] = (start + timedelta(minutes=minutes)).isoformat()
    reports, _reader, info, _clock = service(config, store, source)
    query = DashboardQuery(
        window=TimeRange(start=start + timedelta(minutes=45), end=start + timedelta(minutes=125))
    )
    run = reports.start(ReportStartRequest(request_id=uuid4(), query=query), USER)
    calls = source.calls
    result = complete(reports, run.id)
    assert result.input_kind == "read-model"
    assert result.result.read_model_version == info.generation
    assert result.result.requests == 2
    assert result.result.usage.prompt == 200
    assert result.result.scanned_records == 1
    assert source.calls == calls
    csv = finops_csv(query, result.result)["content"]
    assert info.generation in csv and "read-model" in csv


def test_maintained_report_can_explicitly_choose_the_original_exhaustive_source_scan(config, store):
    source = Source(22)
    reports, _, _, _ = service(config, store, source)
    query = DashboardQuery(window=TimeRange(start=NOW - timedelta(days=2), end=NOW))
    run = reports.start(ReportStartRequest(request_id=uuid4(), query=query, input_kind="source-scan"), USER)
    result = complete(reports, run.id)
    assert result.result.requests == 22
    assert result.result.input_kind == "source-scan"
    assert result.result.read_model_version is None


def test_incomplete_maintained_generation_is_not_an_empty_successful_report(config, store):
    source = Source(5)
    analytics, data, _worker, clock = setup(config, source)
    reader = AnalyticsReader(analytics, source_fingerprint(config), clock=clock)
    reports = FinOpsReportService(store, data, analytics=reader)
    query = DashboardQuery(window=TimeRange(start=NOW - timedelta(days=1), end=NOW))
    from citadel_agent.analytics.snapshots import AnalyticsUnavailable

    with pytest.raises(AnalyticsUnavailable):
        reports.start(ReportStartRequest(request_id=uuid4(), query=query), USER)


def test_private_report_api_selects_maintained_input_and_rechecks_financial_grants(config, store):
    source = Source(4)
    _reports, reader, info, _clock = service(config, store, source)
    client = report_client(config, store, source, analytics=reader)
    query = DashboardQuery(window=TimeRange(start=NOW - timedelta(days=1), end=NOW))
    body = {"requestId": str(uuid4()), "query": query.to_param(), "inputKind": "read-model"}
    accepted = client.post("/internal/finops/reports", json=body)
    assert accepted.status_code == 202
    run = accepted.json()
    assert run["readModelVersion"] == info.generation
    path = "/internal/finops/reports/" + run["id"]
    assert client.post(path + "/advance").json()["status"] == "succeeded"
    csv = client.post(path + "/export").json()["content"]
    assert info.generation in csv and "read-model" in csv
    denied = report_client(config, store, source, analytics=reader, permission=False)
    assert denied.get(path).status_code == 403
    viewer = report_client(
        config,
        store,
        source,
        analytics=reader,
        user=identity(roles=frozenset({PortalRole.VIEWER})),
    )
    assert viewer.post("/internal/finops/reports", json=body).status_code == 403


def test_transient_maintained_read_failure_preserves_the_last_committed_page(config, store, monkeypatch):
    source = Source(100)
    reports, reader, _, _clock = service(config, store, source)
    instant = NOW - timedelta(hours=1)
    query = DashboardQuery(
        window=TimeRange(start=instant - timedelta(seconds=1), end=instant + timedelta(seconds=1))
    )
    run = reports.start(ReportStartRequest(request_id=uuid4(), query=query), USER)
    page = reader.store.page
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HttpResponseError(message="Private metadata failure must not leak.")
        return page(*args, **kwargs)

    monkeypatch.setattr(reader.store, "page", fail_second)
    with pytest.raises(ReportUnavailable):
        reports.advance(run.id, USER)
    saved = reports.get(run.id, USER)
    assert saved.status == "pending"
    assert saved.matched_requests == 20
    assert saved.error_code == "report-source-unavailable"
    assert complete(reports, run.id).result.requests == 100
