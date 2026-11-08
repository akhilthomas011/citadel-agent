"""Resume complete-source reports without double-counting or presenting partial totals."""

import copy
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from azure.core.exceptions import HttpResponseError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from citadel_agent.azure_clients.cosmos import CosmosQueryPage
from citadel_agent.portal.authz import PortalGrantPermission
from citadel_agent.portal.contracts import ObservationState, PortalRole, TimeRange
from citadel_agent.portal.data import PortalDataService
from citadel_agent.portal.grants import VersionedHubGrant
from citadel_agent.portal.product_api import PortalProduct, create_product_router, register_product_errors
from citadel_agent.portal.product_contracts import DashboardQuery, ReportStartRequest
from citadel_agent.portal.reports import FinOpsReportService, ReportUnavailable
from citadel_agent.portal.sessions import PortalStateConflict
from citadel_agent.portal.state import ProductRecordTooLarge
from test_portal_product import (
    NOW,
    OTHER,
    USER,
    grant,
    identity,
    query,
    usage_document,
)
from test_portal_product import (
    config as config_fixture,
)
from test_portal_product import (
    store as store_fixture,
)

config = config_fixture
store = store_fixture


class PagedUsage:
    def __init__(self, total=1100, *, historical=False):
        self.total = total
        self.historical = historical
        self.prices = []
        self.pricing_calls = 0
        self.source_calls = 0
        self.fail_offset = None
        self.hook = None

    def read_feed_ranges(self, database, container):
        return ({"test-range": "all"},)

    def query_page(
        self, database, container, sql, parameters, *, page_size, continuation_token, feed_range=None
    ):
        if container == "model-pricing":
            self.pricing_calls += 1
            return CosmosQueryPage(tuple(copy.deepcopy(self.prices)), None)
        assert parameters["gateway"] == "test-apim"
        assert isinstance(parameters["cutoff"], int)
        assert "@gateway" in sql and "@cutoff" in sql
        self.source_calls += 1
        offset = int(continuation_token) if continuation_token else 0
        if self.fail_offset == offset:
            self.fail_offset = None
            raise HttpResponseError(message="Source failure must not leak its connection information.")
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        end = min(offset + page_size, self.total)
        documents = []
        for index in range(offset, end):
            event_time = NOW - timedelta(hours=1)
            if self.historical and index == self.total - 1:
                event_time = NOW - timedelta(days=39, hours=12)
            documents.append(
                usage_document(id=str(index), timestamp=event_time.isoformat(), _ts=int(NOW.timestamp()))
            )
        return CosmosQueryPage(tuple(documents), str(end) if end < self.total else None)


def services(config, store, reader):
    data = PortalDataService(config, SimpleNamespace(), usage_reader=reader, logs=SimpleNamespace())
    return data, FinOpsReportService(store, data)


def start(service, report_query=None):
    return service.start(ReportStartRequest(request_id=uuid4(), query=report_query or query()), USER)


def finish(service, report_id):
    for _ in range(100):
        report = service.advance(report_id, USER)
        if report.status in {"succeeded", "failed", "cancelled"}:
            return report
    pytest.fail("A finite source did not finish in the expected bounded advances.")


def test_historical_event_beyond_twenty_thousand_records_is_not_silently_excluded(config, store):
    reader = PagedUsage(20_501, historical=True)
    _, service = services(config, store, reader)
    report_query = DashboardQuery(
        window=TimeRange(start=NOW - timedelta(days=40), end=NOW - timedelta(days=39))
    )
    report = start(service, report_query)
    result = finish(service, report.id)
    assert result.status == "succeeded"
    assert result.scanned_records == 20_501
    assert result.result.requests == 1
    assert result.result.usage.prompt == 100
    assert result.result.source_scan_complete is True
    assert result.result.truncated is False
    assert result.result.quality.state is ObservationState.UNKNOWN
    assert result.result.quality.watermark is None
    assert "source-read-is-not-transactional-snapshot" in result.result.quality.warning_codes
    assert reader.pricing_calls == 1
    assert reader.source_calls == 42


def test_checkpoint_retry_and_process_recreation_do_not_double_count(config, store):
    reader = PagedUsage(1100)
    reader.fail_offset = 500
    data, service = services(config, store, reader)
    report = start(service)
    with pytest.raises(ReportUnavailable):
        service.advance(report.id, USER)
    saved = service.get(report.id, USER)
    assert saved.status == "pending"
    assert saved.scanned_records == 500
    assert saved.result is None
    resumed = FinOpsReportService(store, data)
    result = finish(resumed, report.id)
    assert result.status == "succeeded"
    assert result.result.requests == 1100
    assert result.result.usage.prompt == 110_000
    calls = reader.source_calls
    assert resumed.advance(report.id, USER).result == result.result
    assert reader.source_calls == calls
    assert resumed.recent(USER)[0].result is None


def test_repeated_start_request_is_idempotent_and_query_mismatch_is_rejected(config, store):
    reader = PagedUsage()
    _, service = services(config, store, reader)
    request = ReportStartRequest(request_id=uuid4(), query=query())
    first = service.start(request, USER)
    assert service.start(request, USER).id == first.id
    assert reader.pricing_calls == 1
    changed = request.model_copy(update={"query": query().model_copy(update={"model": "other-model"})})
    with pytest.raises(PortalStateConflict, match="different query"):
        service.start(changed, USER)


def test_oversized_initial_state_is_not_mistaken_for_a_duplicate_or_missing_report(
    config, store, monkeypatch
):
    monkeypatch.setattr("citadel_agent.portal.state.MAX_DOCUMENT_BYTES", 1000)
    _, service = services(config, store, PagedUsage(1))
    with pytest.raises(ProductRecordTooLarge, match="too large"):
        start(service)
    assert store.container.items == {}
    client = report_client(config, store, PagedUsage(1))
    response = client.post(
        "/internal/finops/reports",
        json={"requestId": str(uuid4()), "query": query().to_param()},
    )
    assert response.status_code == 409
    assert "too large" in response.json()["detail"]


def test_checkpoint_conflict_is_distinct_from_a_document_size_failure(config, store, monkeypatch):
    _, service = services(config, store, PagedUsage(1))
    report = start(service)
    replace = store.replace

    def reject_progress(current, data):
        if data["scannedRecords"]:
            raise PortalStateConflict("Checkpoint conflict fixture.")
        return replace(current, data)

    monkeypatch.setattr(store, "replace", reject_progress)
    failed = service.advance(report.id, USER)
    assert failed.status == "failed"
    assert failed.error_code == "report-checkpoint-conflict"
    assert failed.result is None


def test_pricing_is_frozen_for_the_report_and_all_unpriced_usage_remains_visible(config, store):
    reader = PagedUsage(1501)
    reader.prices = [
        {
            "id": "reviewed-rate",
            "model": "gpt-test",
            "deploymentName": "test-deployment",
            "isActive": True,
            "CostUnit": 1000,
            "CostPerInputUnit": 2,
            "CostPerOutputUnit": 4,
            "Currency": "USD",
            "CalculationMethod": "tokens",
            "effective_from": (NOW - timedelta(days=100)).isoformat(),
        }
    ]
    _, service = services(config, store, reader)
    report = start(service)
    reader.prices[0]["CostPerInputUnit"] = 200
    complete = finish(service, report.id)
    assert complete.result.costs[0].amount == Decimal("600.4")
    assert complete.result.priced_usage_percent == 100
    assert reader.pricing_calls == 1
    reader.prices = []
    unpriced = finish(service, start(service).id)
    assert unpriced.result.costs == []
    assert unpriced.result.unpriced_requests == 1501
    assert unpriced.result.requests == 1501


def test_concurrent_advances_and_cancel_cannot_overwrite_committed_progress(config, store):
    reader = PagedUsage(600)
    data, service = services(config, store, reader)
    report = start(service)
    observed = []
    reader.hook = lambda: observed.append(FinOpsReportService(store, data).advance(report.id, USER))
    result = finish(service, report.id)
    assert observed[0].status == "running"
    assert observed[0].scanned_records == 0
    assert result.result.requests == 600
    assert reader.source_calls == 2
    another = start(service)
    reader.hook = lambda: service.cancel(another.id, USER)
    cancelled = service.advance(another.id, USER)
    assert cancelled.status == "cancelled"
    assert cancelled.result is None
    with pytest.raises(PortalStateConflict, match="completed"):
        service.completed(another.id, USER)


def test_report_owner_scope_and_source_fingerprint_are_rechecked(config, store):
    reader = PagedUsage()
    _data, service = services(config, store, reader)
    report = start(service)
    for action in (service.get, service.advance, service.cancel, service.completed):
        with pytest.raises(KeyError):
            action(report.id, OTHER)
    assert service.recent(OTHER) == []
    changed = config.model_copy(update={"usage_database": "another-database"})
    other_data = PortalDataService(changed, SimpleNamespace(), usage_reader=reader, logs=SimpleNamespace())
    with pytest.raises(PortalStateConflict, match="source configuration changed"):
        FinOpsReportService(store, other_data).advance(report.id, USER)
    assert reader.source_calls == 0


def test_report_response_never_exposes_source_continuation_or_connection_targets(config, store, monkeypatch):
    monkeypatch.setattr("citadel_agent.portal.reports.time", SimpleNamespace(monotonic=lambda: 0))
    reader = PagedUsage(2500)
    _, service = services(config, store, reader)
    report = start(service)
    progress = service.advance(report.id, USER)
    assert progress.status == "pending"
    assert progress.scanned_records == 2000
    assert progress.result is None
    exposed = progress.model_dump_json(by_alias=True)
    assert "continuation" not in exposed
    assert "usage.documents.azure.com" not in exposed
    assert "checkpoint" not in exposed and "CostPerInputUnit" not in exposed
    with pytest.raises(PortalStateConflict):
        service.completed(report.id, USER)


def test_resume_after_expired_batch_lease_is_safe(config, store):
    _, service = services(config, store, PagedUsage(2))
    report = start(service)
    current = store.get("finops-report", report.id)
    data = copy.deepcopy(current.record.data)
    data.update(status="running", leaseUntil=(NOW - timedelta(minutes=5)).isoformat())
    store.replace(current, data)
    assert finish(service, report.id).result.requests == 2


def test_all_opaque_feed_ranges_must_be_exhausted_including_empty_pages(config, store):
    class RangedUsage(PagedUsage):
        def read_feed_ranges(self, database, container):
            return (
                ({"opaque": "empty"}, {"opaque": "populated"})
                if container == "llm-usage-container"
                else ({"opaque": "prices"},)
            )

        def query_page(
            self, database, container, sql, parameters, *, page_size, continuation_token, feed_range=None
        ):
            if container == "model-pricing":
                return CosmosQueryPage((), None)
            self.source_calls += 1
            if feed_range == {"opaque": "empty"}:
                return CosmosQueryPage((), "empty-page-next" if continuation_token is None else None)
            assert feed_range == {"opaque": "populated"}
            assert continuation_token is None
            return CosmosQueryPage((usage_document(id="final-range"),), None)

    reader = RangedUsage()
    _, service = services(config, store, reader)
    report = finish(service, start(service).id)
    assert report.status == "succeeded"
    assert report.result.requests == 1
    assert reader.source_calls == 3


@pytest.mark.parametrize("record_limit", [1_500_000, 10_000])
def test_cardinality_or_document_limit_fails_explicitly_without_publishing_partial_totals(
    config, store, monkeypatch, record_limit
):
    monkeypatch.setattr("citadel_agent.portal.state.MAX_DOCUMENT_BYTES", record_limit)

    class WideUsage(PagedUsage):
        def query_page(self, *args, **kwargs):
            page = super().query_page(*args, **kwargs)
            if args[1] == "model-pricing":
                return page
            return CosmosQueryPage(
                tuple({**document, "model": f"model-{document['id']}"} for document in page.items),
                page.continuation_token,
            )

    _, service = services(config, store, WideUsage(1100))
    report = finish(service, start(service).id)
    assert report.status == "failed"
    assert report.error_code in {"report-cardinality-limit", "report-result-too-large"}
    if record_limit == 10_000:
        assert report.error_code == "report-result-too-large"
    assert report.result is None
    with pytest.raises(PortalStateConflict):
        service.completed(report.id, USER)


def test_expired_reports_and_invalid_state_do_not_leak_internal_source_tokens(config, store):
    _, service = services(config, store, PagedUsage(1))
    report = start(service)
    current = store.get("finops-report", report.id)
    stored = store.container.items[current.record.id]
    original = copy.deepcopy(stored)
    stored["data"]["version"] = 999
    stored["data"]["continuation"] = "private-source-token-must-not-leak"
    with pytest.raises(ReportUnavailable) as failure:
        service.get(report.id, USER)
    assert "private-source-token" not in str(failure.value)
    store.container.items[current.record.id] = original
    original["expiresAt"] = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(KeyError):
        service.get(report.id, USER)


def test_only_normalized_aggregates_not_source_bodies_or_addresses_are_persisted(config, store):
    class PrivateUsage(PagedUsage):
        def query_page(self, *args, **kwargs):
            page = super().query_page(*args, **kwargs)
            if args[1] == "model-pricing":
                return page
            return CosmosQueryPage(
                tuple(
                    {
                        **document,
                        "requestBody": "private-prompt-fixture",
                        "RequestIp": "private-address-fixture",
                    }
                    for document in page.items
                ),
                page.continuation_token,
            )

    _, service = services(config, store, PrivateUsage(1))
    report = finish(service, start(service).id)
    assert report.result.requests == 1
    persisted = json.dumps(store.container.items)
    assert "private-prompt-fixture" not in persisted and "private-address-fixture" not in persisted


def report_client(config, store, reader, *, user=None, permission=True, permissions=None, analytics=None):
    user = user or identity()
    user_grant = grant(config, user.subject_id)
    if permissions is not None:
        user_grant = user_grant.model_copy(update={"permissions": permissions})
    grants = SimpleNamespace(get=lambda *_: VersionedHubGrant(user_grant, "etag") if permission else None)
    data, _ = services(config, store, reader)
    product = PortalProduct(
        config=config,
        grant_store=grants,
        data=data,
        assistant=SimpleNamespace(store=store, durable_turns=False),
        analytics=analytics,
    )

    def context():
        return SimpleNamespace(user=user)

    app = FastAPI()
    app.include_router(create_product_router(product, context))
    register_product_errors(app)
    return TestClient(app)


def test_completed_report_export_uses_exact_result_and_rechecks_roles(config, store):
    reader = PagedUsage(2)
    client = report_client(config, store, reader)
    response = client.post(
        "/internal/finops/reports",
        json={
            "requestId": str(uuid4()),
            "query": query().to_param(),
        },
    )
    assert response.status_code == 202
    report_id = response.json()["id"]
    path = f"/internal/finops/reports/{report_id}"
    assert client.post(path + "/export").status_code == 409
    completed = client.post(path + "/advance").json()
    assert completed["status"] == "succeeded"
    reader.total = 100
    export = client.post(path + "/export")
    assert export.status_code == 200
    csv = export.json()["content"]
    assert report_id in csv
    assert "200,100" in csv
    assert reader.source_calls == 1
    viewer = report_client(config, store, reader, user=identity(roles=frozenset({PortalRole.VIEWER})))
    assert viewer.get(path).status_code == 403
    other = report_client(config, store, reader, user=identity(OTHER))
    assert other.get(path).status_code == 404
    assert other.post(path + "/export").status_code == 404
    assert "continuation" not in json.dumps(completed)


def test_completed_reports_recheck_revoked_finops_and_export_grants(config, store):
    reader = PagedUsage(1)
    client = report_client(config, store, reader)
    body = {"requestId": str(uuid4()), "query": query().to_param()}
    report_id = client.post("/internal/finops/reports", json=body).json()["id"]
    path = f"/internal/finops/reports/{report_id}"
    assert client.post(path + "/advance").json()["status"] == "succeeded"
    no_export = report_client(
        config, store, reader, permissions=frozenset({PortalGrantPermission.FINOPS_DATA})
    )
    assert no_export.get(path).status_code == 200
    assert no_export.post(path + "/export").status_code == 403
    revoked = report_client(config, store, reader, permission=False)
    for method, route in [
        ("GET", "/internal/finops/reports"),
        ("GET", path),
        ("POST", path + "/advance"),
        ("POST", path + "/cancel"),
        ("POST", path + "/export"),
    ]:
        assert revoked.request(method, route).status_code == 403
    assert revoked.post("/internal/finops/reports", json=body).status_code == 403
    assert reader.source_calls == 1
