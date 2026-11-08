"""Operational sources distinguish denied/empty/stale evidence without leaking request content."""

import json
import re
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from citadel_agent.azure_clients.monitor import AlertReader, MonitorReadError, QueryResult
from citadel_agent.portal.contracts import ObservationState
from citadel_agent.portal.data import PortalDataService
from citadel_agent.portal.operations import OperationsService
from citadel_agent.portal.product_contracts import OperationsQuery, RequestEvidenceQuery
from test_finops_reports import report_client
from test_portal_product import NOW, query
from test_portal_product import config as config_fixture
from test_portal_product import store as store_fixture

config = config_fixture
store = store_fixture


class Logs:
    def __init__(self, rows=(), *, partial=False, error=None):
        self.rows, self.partial, self.error = list(rows), partial, error
        self.queries = []

    def query_workspace(self, workspace, kql, window):
        self.queries.append(kql)
        if self.error:
            raise self.error
        return QueryResult(self.rows, self.partial)


def services(config, logs, *, alerts=None, analytics=None):
    configured = config.model_copy(
        update={"log_analytics_workspace_id": "33333333-3333-3333-3333-333333333333"}
    )
    data = PortalDataService(configured, SimpleNamespace(), usage_reader=SimpleNamespace(), logs=logs)
    return data, OperationsService(data, alerts=alerts, analytics=analytics)


def test_gateway_filters_use_real_columns_and_do_not_weaken_resource_or_time_scope(config):
    logs = Logs(
        [{"kind": "total", "requests": 0, "failures": 0, "throttled": 0, "authFailures": 0, "p95Ms": None}]
    )
    data, _ = services(config, logs)
    value = 'backend" | take 100 | //'
    response = data.operations(
        OperationsQuery(window=query().window, gateway_filters={"backend": value, "region": "eastus"})
    )
    kql = logs.queries[0]
    assert config.apim_resource_id in kql
    assert "TimeGenerated >=" in kql and "TimeGenerated <" in kql
    assert "BackendId == " + json.dumps(value) in kql
    assert 'Region == "eastus"' in kql
    assert "percentile(TotalTime,95)" in kql
    assert response.quality.state is ObservationState.UNKNOWN
    assert "no-observed-traffic-is-not-verified-health" in response.warnings


@pytest.mark.parametrize("code", [403, 429, 503])
def test_evidence_denial_and_outage_are_not_empty_success(config, code):
    _, service = services(config, Logs(error=MonitorReadError(code)))
    result = service.evidence(RequestEvidenceQuery(window=query().window, correlation_id=uuid4()))
    assert result.records == [] and result.quality.state is ObservationState.UNAVAILABLE
    assert any(value.startswith("request-evidence-") for value in result.warnings)


def test_evidence_is_exactly_scoped_and_excludes_all_unprojected_content(config):
    cid = str(uuid4())
    logs = Logs(
        [
            {
                "timestamp": NOW - timedelta(hours=1),
                "correlation_id": cid,
                "response_code": 502,
                "backend_response_code": 503,
                "total_ms": 150,
                "backend_ms": 140,
                "backend": "b1",
                "region": "eastus",
            }
        ]
    )
    _, service = services(config, logs)
    result = service.evidence(RequestEvidenceQuery(window=query().window, correlation_id=cid))
    assert result.quality.state is ObservationState.HEALTHY
    assert result.records[0].total_ms == Decimal(150)
    assert "| project timestamp=" in logs.queries[0]
    assert "RequestBody" not in logs.queries[0] and "CallerIpAddress" not in logs.queries[0]
    logs.rows[0]["requestBody"] = "private-prompt-not-returned"
    result = service.evidence(RequestEvidenceQuery(window=query().window, correlation_id=cid))
    assert result.quality.state is ObservationState.UNAVAILABLE
    assert "private-prompt-not-returned" not in result.model_dump_json()


def test_evidence_rejects_mismatched_correlation_and_marks_partial_results(config):
    cid = str(uuid4())
    logs = Logs([{"timestamp": NOW - timedelta(hours=1), "correlation_id": str(uuid4())}])
    _, service = services(config, logs)
    result = service.evidence(RequestEvidenceQuery(window=query().window, correlation_id=cid))
    assert result.records == [] and "invalid-request-evidence" in result.warnings
    logs.rows[0]["correlation_id"] = cid
    logs.partial = True
    result = service.evidence(RequestEvidenceQuery(window=query().window, correlation_id=cid))
    assert result.truncated and result.quality.state is ObservationState.PARTIAL


def test_alert_reader_only_reads_filtered_instances_and_never_follows_untrusted_next_links(config):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["targetResource"] == config.apim_resource_id
        assert request.url.params["includeContext"] == "false"
        assert request.url.params["monitorCondition"] == "Fired"
        assert request.method == "GET"
        return httpx.Response(
            200, json={"value": [], "nextLink": "https://not-a-monitor-host.example/private"}
        )

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="synthetic-token"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reader = AlertReader(credential, client=client)
        result = reader.read(
            config.scope.subscription_id, config.apim_resource_id, query().window.start, query().window.end
        )
    assert result.partial and len(requests) == 1


def test_active_alerts_reject_another_resource_and_do_not_return_context(config):
    alert_id = (
        f"/subscriptions/{config.scope.subscription_id}/providers/Microsoft.AlertsManagement/alerts/{uuid4()}"
    )
    raw = {
        "id": alert_id,
        "properties": {
            "context": {"private": "do-not-export"},
            "essentials": {
                "targetResource": config.apim_resource_id,
                "severity": "Sev2",
                "monitorCondition": "Fired",
                "alertState": "New",
                "startDateTime": NOW.isoformat(),
                "lastModifiedDateTime": NOW.isoformat(),
            },
        },
    }
    alerts = SimpleNamespace(read=lambda *args: QueryResult([raw]))
    _, service = services(config, Logs(), alerts=alerts)
    result = service.alert_instances(query())
    assert len(result.alerts) == 1
    assert "do-not-export" not in result.model_dump_json()
    raw["properties"]["essentials"]["targetResource"] = config.apim_resource_id + "-another"
    result = service.alert_instances(query())
    assert result.alerts == [] and result.quality.state is ObservationState.UNAVAILABLE


@pytest.mark.parametrize(
    ("age", "partial", "expected"),
    [
        (timedelta(minutes=1), False, "healthy"),
        (timedelta(hours=1), False, "stale"),
        (timedelta(minutes=1), True, "partial"),
    ],
)
def test_component_telemetry_freshness_is_not_pipeline_completeness(config, age, partial, expected):
    resource = (
        f"/subscriptions/{config.scope.subscription_id}/resourceGroups/source"
        "/providers/Microsoft.EventHub/namespaces/usage"
    )
    config = config.model_copy(update={"pipeline_resources": {"event_hub": resource}})
    logs = Logs([{"MetricName": "IncomingMessages", "observed": NOW - age, "total": 25}], partial=partial)
    _, service = services(config, logs)
    result = service.pipeline_health(query())
    assert result.components[0].state == expected
    assert result.quality.state is not ObservationState.HEALTHY
    assert "component-metrics-are-not-an-ingestion-watermark" in result.warnings
    assert resource in logs.queries[0]


def test_pipeline_empty_and_permission_denied_remain_distinct(config):
    resource = (
        f"/subscriptions/{config.scope.subscription_id}/resourceGroups/source"
        "/providers/Microsoft.EventHub/namespaces/usage"
    )
    config = config.model_copy(update={"pipeline_resources": {"event_hub": resource}})
    logs = Logs()
    _, service = services(config, logs)
    assert service.pipeline_health(query()).components[0].state == "unknown"
    logs.error = MonitorReadError(403)
    result = service.pipeline_health(query())
    assert result.components[0].state == "unavailable"
    assert "component-permission-denied" in result.components[0].warnings


def test_request_evidence_api_rechecks_the_extra_raw_logs_grant(config, store):
    from citadel_agent.portal.authz import PortalGrantPermission

    client = report_client(
        config, store, SimpleNamespace(), permissions=frozenset({PortalGrantPermission.FINOPS_DATA})
    )
    response = client.post(
        "/internal/operations/evidence",
        json={
            "window": query().window.model_dump(mode="json"),
            "correlationId": str(uuid4()),
        },
    )
    assert response.status_code == 403


def test_maintained_operational_attribution_uses_exact_server_counts_without_financial_fields(config):
    from citadel_agent.analytics.read_model import AnalyticsReader
    from citadel_agent.analytics.snapshots import source_fingerprint
    from test_analytics_snapshots import Source, finish, setup

    source = Source(4)
    source.documents["0"]["model"] = "model-one"
    source.documents["1"]["model"] = "model-one"
    source.documents["2"]["model"] = "model-two"
    source.documents["3"]["model"] = "model-two"
    store, data, worker, clock = setup(config, source)
    snapshot = finish(worker)
    original = store.container

    class Grouped:
        def __getattr__(self, name):
            return getattr(original, name)

        def query_items(self, query, **kwargs):
            assert "COUNT(1)" in query and "GROUP BY" in query
            assert kwargs["partition_key"] == store.partition(snapshot.generation)
            parameters = {value["name"]: value["value"] for value in kwargs["parameters"]}
            dimension = query.rsplit(".", 1)[1]
            conditions = re.findall(r"dimensions\.([a-z0-9_]+) = (@filter[0-9]+)", query)
            counts = Counter()
            for (partition, _), raw in original.items.items():
                if partition != store.partition(snapshot.generation) or raw["kind"] != "event":
                    continue
                observation = raw["payload"]["observation"]
                if not parameters["@start"] <= observation["eventTime"] < parameters["@end"]:
                    continue
                if any(
                    observation["dimensions"].get(name) != parameters[param] for name, param in conditions
                ):
                    continue
                counts[observation["dimensions"].get(dimension)] += 1
            return [{"value": label, "requests": count} for label, count in counts.items()]

    store.container = Grouped()
    reader = AnalyticsReader(store, source_fingerprint(config), clock=clock)
    service = OperationsService(data, analytics=reader)
    result = service.attribution(query())
    assert {row.value: row.requests for row in result.groups["model"]} == {"model-one": 2, "model-two": 2}
    encoded = result.model_dump_json()
    assert '"costs"' not in encoded and '"prompt"' not in encoded and "customDimension" not in encoded
