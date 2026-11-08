"""Hosted services preserve scope, concurrency, cost semantics and explicit browser approvals."""

import asyncio
import copy
import csv
import io
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from azure.cosmos.exceptions import CosmosAccessConditionFailedError, CosmosResourceNotFoundError
from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient

from citadel_agent.azure_clients.cosmos import CosmosQueryPage
from citadel_agent.azure_clients.monitor import QueryResult
from citadel_agent.changes.changeset import ChangeSet
from citadel_agent.portal.approvals import PortalApprovalService
from citadel_agent.portal.assistant import (
    CosmosChangeStore,
    PortalAssistantService,
    _restore_drafts,
)
from citadel_agent.portal.auth import PortalIdentity
from citadel_agent.portal.authz import HubGrant, PortalGrantPermission
from citadel_agent.portal.contracts import ObservationState, PortalRole, TimeRange
from citadel_agent.portal.data import PortalDataService
from citadel_agent.portal.grants import VersionedHubGrant
from citadel_agent.portal.product_api import PortalProduct, create_product_router, register_product_errors
from citadel_agent.portal.product_contracts import (
    ChangeDecision,
    DashboardQuery,
    PortalHubConfig,
    TurnRequest,
)
from citadel_agent.portal.sessions import PortalStateConflict
from citadel_agent.portal.state import ProductStore
from citadel_agent.portal.tools import hosted_registry, hosted_usage_tool
from citadel_agent.tools.registry import ToolRegistry, ToolSpec
from test_portal_sessions import FakeContainer as SessionContainer

NOW = datetime.now(UTC)
TENANT = "11111111-1111-4111-8111-111111111111"
USER = "e1992d1d-a8d2-48d4-b7a6-80e287879d88"
OTHER = "c2f22c29-1022-4b9f-894a-719f9338aa67"


class Container(SessionContainer):
    def replace_item(self, item, body, partition_key, *, etag=None, match_condition=None):
        if item not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        old = self.items[item]
        if old["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412)
        self.items[item] = {**copy.deepcopy(body), "_etag": str(uuid4())}

    def query_items(self, query, parameters, **kwargs):
        values = {p["name"]: p["value"] for p in parameters}
        return [
            copy.deepcopy(item)
            for item in self.items.values()
            if item["kind"] == values["@kind"]
            and item["scope"]["hubId"] == values["@hub"]
            and item["scope"]["tenantId"] == values["@tenant"]
            and item["scope"]["environment"] == values["@environment"]
            and item["scope"]["subscriptionId"] == values["@subscription"]
            and ("@subject" not in values or item["subjectId"] == values["@subject"])
        ][: values["@limit"]]


@pytest.fixture
def config():
    return PortalHubConfig(
        scope={
            "tenantId": TENANT,
            "hubId": "gateway",
            "environment": "prod",
            "subscriptionId": "22222222-2222-4222-8222-222222222222",
        },
        display_name="Test gateway",
        apim_resource_group="test-rg",
        apim_name="test-apim",
        gateway_url="https://gateway.example",
        contracts_repository_url="https://github.com/example/contracts",
        usage_endpoint="https://usage.documents.azure.com",
        usage_database="usage",
        model_endpoint="https://models.openai.azure.com",
        model_deployment="test-model",
    )


@pytest.fixture
def store(config):
    return ProductStore(Container(), config.scope)


def identity(subject=USER, roles=frozenset(PortalRole)):
    return PortalIdentity(
        subject_id=subject,
        tenant_id=TENANT,
        client_id="test-client",
        expires_at=NOW + timedelta(hours=1),
        scopes=frozenset({"access_as_user"}),
        roles=roles,
    )


def grant(config, subject=USER):
    return HubGrant(subject_id=subject, scope=config.scope, permissions=frozenset(PortalGrantPermission))


def test_product_store_enforces_owner_scope_and_etag(store, config):
    created = store.create("conversation", "test", USER, {"value": 1})
    with pytest.raises(KeyError):
        store.get("conversation", "test", subject_id=OTHER)
    updated = store.replace(created, {"value": 2})
    assert updated.record.data == {"value": 2}
    with pytest.raises(PortalStateConflict):
        store.replace(created, {"value": 3})
    other = config.scope.model_copy(update={"hub_id": "other-hub"})
    with pytest.raises(KeyError):
        ProductStore(store.container, other).get("conversation", "test")
    assert store.list("conversation", subject_id=OTHER) == []


@pytest.mark.asyncio
async def test_conversations_survive_service_recreation_and_retries_are_idempotent(store, config):
    calls = []

    async def runner(data, text, user, user_grant):
        calls.append(text)
        return "Grounded answer", {"session": "persisted"}, {"d-test.yaml": "intent: saved"}

    factory = SimpleNamespace()
    service = PortalAssistantService(store, factory, runner=runner)
    conversation = service.create_conversation(identity())
    request = TurnRequest(request_id=uuid4(), message="Show my models")
    result = await service.turn(conversation.id, request, identity(), grant(config))
    again = await service.turn(conversation.id, request, identity(), grant(config))
    restored = PortalAssistantService(store, factory, runner=runner).conversation(conversation.id, identity())
    assert len(calls) == 1
    assert len(restored.messages) == 2
    assert again.messages == result.messages == restored.messages
    with pytest.raises(PortalStateConflict, match="different message"):
        await service.turn(
            conversation.id,
            request.model_copy(update={"message": "Different request"}),
            identity(),
            grant(config),
        )
    with pytest.raises(KeyError):
        service.conversation(conversation.id, identity(OTHER))


@pytest.mark.asyncio
async def test_deferred_turn_returns_before_model_runs_and_duplicate_submission_does_not_schedule_twice(
    store, config
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def runner(data, text, user, user_grant):
        entered.set()
        await release.wait()
        return "Deferred answer", {}, {}

    service = PortalAssistantService(store, SimpleNamespace(), runner=runner)
    conversation = service.create_conversation(identity())
    request = TurnRequest(request_id=uuid4(), message="A long-running gateway task")
    tasks = BackgroundTasks()
    accepted = await service.turn(conversation.id, request, identity(), grant(config), background_tasks=tasks)
    assert accepted.status == "running"
    assert accepted.messages[-1].content == request.message
    assert not entered.is_set()
    duplicate = await service.turn(
        conversation.id, request, identity(), grant(config), background_tasks=tasks
    )
    assert duplicate.status == "running"
    assert len(tasks.tasks) == 1
    worker = asyncio.create_task(tasks())
    await asyncio.wait_for(entered.wait(), 1)
    assert service.conversation(conversation.id, identity()).status == "running"
    release.set()
    await asyncio.wait_for(worker, 1)
    finished = service.conversation(conversation.id, identity())
    assert finished.status == "idle"
    assert [message.content for message in finished.messages] == [request.message, "Deferred answer"]


@pytest.mark.asyncio
async def test_turn_rejects_secrets_and_concurrent_turns_before_model_call(store, config):
    async def unexpected(*args):
        pytest.fail("model must not be invoked")

    service = PortalAssistantService(store, SimpleNamespace(), runner=unexpected)
    conversation = service.create_conversation(identity())
    rejected_value = "0123456789abcdef" * 2
    request = TurnRequest(request_id=uuid4(), message=f"api-key: {rejected_value}")
    with pytest.raises(ValueError, match="secrets"):
        await service.turn(conversation.id, request, identity(), grant(config))
    current = store.get("conversation", conversation.id)
    data = {
        **current.record.data,
        "status": "running",
        "leaseUntil": (NOW + timedelta(minutes=5)).isoformat(),
    }
    store.replace(current, data)
    with pytest.raises(PortalStateConflict, match="running"):
        await service.turn(
            conversation.id, TurnRequest(request_id=uuid4(), message="Hello"), identity(), grant(config)
        )


def test_saved_drafts_cannot_escape_temporary_state(tmp_path):
    with pytest.raises(ValueError, match="draft path"):
        _restore_drafts(tmp_path, {"../d-secret.yaml": "data"})
    _restore_drafts(tmp_path, {"d-test.yaml": "saved"})
    assert (tmp_path / "d-test.yaml").read_text() == "saved"


class Usage:
    def __init__(self, documents, prices=(), *, truncated=False):
        self.documents = documents
        self.prices = list(prices)
        self.calls = []
        self.truncated = truncated

    def read_feed_ranges(self, database, container):
        return ({"test-range": "all"},)

    def query_page(
        self, database, container, sql, parameters, *, page_size, continuation_token, feed_range=None
    ):
        self.calls.append((container, sql, parameters, page_size))
        if container == "model-pricing":
            return CosmosQueryPage(tuple(self.prices), None)
        if continuation_token:
            return CosmosQueryPage((), None)
        return CosmosQueryPage(tuple(self.documents[:page_size]), "more" if self.truncated else None)


def usage_document(**overrides):
    return {
        "id": "llm-test",
        "timestamp": (NOW - timedelta(hours=1)).isoformat(),
        "gatewayName": "test-apim",
        "model": "gpt-test",
        "deploymentName": "test-deployment",
        "gatewayRegion": "ALL",
        "promptTokens": "100",
        "responseTokens": "50",
        "promptCachedTokens": "0",
        "promptAudioTokens": "0",
        "promptImageTokens": "0",
        "promptCachedImageTokens": "0",
        "completionAudioTokens": "0",
        "completionReasoningTokens": "0",
        **overrides,
    }


def query():
    return DashboardQuery(window=TimeRange(start=NOW - timedelta(days=2), end=NOW))


def test_finops_reads_real_sources_and_preserves_missing_prices_and_totals(config):
    document = usage_document()
    document.pop("promptCachedTokens")
    reader = Usage([document])
    service = PortalDataService(config, SimpleNamespace(), usage_reader=reader, logs=SimpleNamespace())
    report = service.finops(query())
    assert report.requests == 1
    assert report.usage.prompt == 100
    assert report.usage.total is None
    assert report.costs == []
    assert report.unpriced_requests == 1
    assert report.priced_usage_percent == 0
    assert report.quality.state is ObservationState.UNKNOWN
    assert "usage-field-unavailable:promptCachedTokens" in report.quality.warning_codes
    assert reader.calls[0][2] == {"gateway": "test-apim"}
    assert "test-apim" not in reader.calls[0][1]


def test_hosted_usage_tool_uses_dashboard_pricing_and_quality_semantics(config):
    data = PortalDataService(
        config, SimpleNamespace(), usage_reader=Usage([usage_document()]), logs=SimpleNamespace()
    )
    output = hosted_usage_tool(data).function(days=7, group_by="model")
    report = json.loads(output.split("\nUse the FinOps page", 1)[0])
    assert report["costs"] == []
    assert report["unpricedRequests"] == 1
    assert report["quality"]["state"] == "unknown"
    assert report["scope"] == config.scope.model_dump(by_alias=True, mode="json")


def test_assistant_tool_surface_matches_api_roles_and_sensitive_grants(config):
    calls = []
    registry = ToolRegistry()
    for name in ("search_docs", "gateway_logs", "change_status", "execute_change", "gateway_overview"):
        registry.add(ToolSpec(name, lambda: "result", "test function"))
    audit = SimpleNamespace(record=lambda *args, **kwargs: calls.append((args, kwargs)))
    viewer = hosted_registry(
        registry, identity(roles=frozenset({PortalRole.VIEWER})), grant(config), data=None, audit=audit
    )
    assert "execute_change" not in viewer.specs
    assert "change_status" not in viewer.specs
    assert "gateway_logs" in viewer.specs
    assert viewer.specs["search_docs"].function() == "result"
    assert len(calls) == 2
    assert all("arguments" not in kwargs for _, kwargs in calls)
    analyst = hosted_registry(
        registry,
        identity(roles=frozenset({PortalRole.FINOPS_ANALYST})),
        grant(config),
        data=None,
        audit=audit,
    )
    assert "gateway_logs" not in analyst.specs
    assert "gateway_overview" not in analyst.specs
    assert set(analyst.specs) == {"search_docs"}


def test_finops_effective_rates_and_unpriced_records_are_not_dropped(config):
    prices = [
        {
            "id": "p",
            "model": "gpt-test",
            "deploymentName": "test-deployment",
            "isActive": True,
            "CostUnit": 1000,
            "CostPerInputUnit": 2,
            "CostPerOutputUnit": 4,
            "Currency": "USD",
            "CalculationMethod": "tokens",
            "effective_from": (NOW - timedelta(days=3)).isoformat(),
        }
    ]
    reader = Usage(
        [usage_document(), usage_document(id="unpriced", model="other", deploymentName="other")], prices
    )
    service = PortalDataService(config, SimpleNamespace(), usage_reader=reader, logs=SimpleNamespace())
    report = service.finops(query())
    assert report.costs[0].amount == Decimal("0.4")
    assert report.costs[0].basis.value == "usage-estimate"
    assert report.requests == 2
    assert report.unpriced_requests == 1
    assert report.priced_usage_percent == 50


def test_finops_truncation_is_explicit_and_bad_records_are_counted(config):
    documents = [usage_document(id=str(index)) for index in range(101)]
    documents[0] = {"id": "invalid"}
    config = config.model_copy(update={"max_usage_records": 100})
    service = PortalDataService(
        config, SimpleNamespace(), usage_reader=Usage(documents, truncated=True), logs=SimpleNamespace()
    )
    report = service.finops(query())
    assert report.truncated
    assert report.rejected_records == 1
    assert report.requests == 99
    assert report.quality.state is ObservationState.PARTIAL
    assert "usage-scan-limit-reached" in report.warnings


def test_operations_does_not_claim_zero_for_missing_telemetry(config):
    service = PortalDataService(config, SimpleNamespace(), usage_reader=Usage([]), logs=SimpleNamespace())
    report = service.operations(query())
    assert report.quality.state is ObservationState.UNAVAILABLE
    assert all(metric.value is None for metric in report.metrics)
    assert "log-analytics-not-configured" in report.warnings


def test_operations_scopes_kql_and_uses_global_not_averaged_percentiles(config):
    class Logs:
        def query_workspace(self, workspace, kql, timespan):
            assert config.apim_resource_id in kql
            assert "TimeGenerated >=" in kql and "TimeGenerated <" in kql
            assert "percentile(TotalTime,95)" in kql
            assert timespan == (query().window.start, query().window.end)
            return QueryResult(
                rows=[
                    {
                        "kind": "total",
                        "requests": 100,
                        "failures": 2,
                        "throttled": 3,
                        "authFailures": 1,
                        "p95Ms": 900,
                    },
                    {
                        "kind": "bucket",
                        "timestamp": NOW,
                        "requests": 100,
                        "failures": 2,
                        "throttled": 3,
                        "p95Ms": 900,
                    },
                ]
            )

    config = config.model_copy(update={"log_analytics_workspace_id": TENANT})
    service = PortalDataService(config, SimpleNamespace(), usage_reader=Usage([]), logs=Logs())
    report = service.operations(query())
    assert next(m.value for m in report.metrics if m.name == "latency-p95") == 900
    assert next(m.value for m in report.metrics if m.name == "non-server-error-rate") == 98
    assert len(report.timeline) == 1


def product_client(
    config,
    store,
    *,
    roles=frozenset(PortalRole),
    permissions=frozenset(PortalGrantPermission),
    documents=None,
):
    user = identity(roles=roles)
    user_grant = HubGrant(subject_id=USER, scope=config.scope, permissions=permissions)
    grants = SimpleNamespace(get=lambda *_: VersionedHubGrant(user_grant, "etag"))
    data = PortalDataService(
        config,
        SimpleNamespace(),
        usage_reader=Usage([usage_document()] if documents is None else documents),
        logs=SimpleNamespace(),
    )
    service = PortalAssistantService(store, SimpleNamespace())
    product = PortalProduct(config=config, grant_store=grants, data=data, assistant=service)

    def resolve():
        return SimpleNamespace(user=user)

    app = FastAPI()
    app.include_router(create_product_router(product, resolve))
    register_product_errors(app)
    return TestClient(app)


def test_product_routes_recheck_sensitive_roles_and_export_grants(config, store):
    viewer = product_client(config, store, roles=frozenset({PortalRole.VIEWER}))
    assert viewer.post("/internal/finops/query", json=query().to_param()).status_code == 403
    analyst = product_client(config, store, permissions=frozenset({PortalGrantPermission.FINOPS_DATA}))
    assert analyst.post("/internal/finops/query", json=query().to_param()).status_code == 200
    assert analyst.post("/internal/finops/export", json=query().to_param()).status_code == 403
    operator = product_client(config, store)
    assert operator.post("/internal/finops/export", json=query().to_param()).status_code == 200
    invalid = operator.post("/internal/conversations/../turns", json={"message": "secret"})
    assert invalid.status_code in {404, 405}


def test_finops_export_escapes_spreadsheet_formulas_without_exposing_raw_documents(config, store):
    document = usage_document(model="=1+1", RequestIp="private-ip-not-exported", requestBody="private-body")
    client = product_client(config, store, documents=[document])
    response = client.post("/internal/finops/export", json=query().to_param())
    assert response.status_code == 200
    content = response.json()["content"]
    rows = list(csv.reader(io.StringIO(content)))
    assert rows[1][3] == "'=1+1"
    assert "private-ip-not-exported" not in content
    assert "private-body" not in content
    assert any(item["kind"] == "portal-audit" for item in store.container.items.values())


def test_product_routes_reject_browser_scope_and_excessive_time_windows(config, store):
    client = product_client(config, store)
    body = query().to_param()
    body["scope"] = config.scope.to_param()
    response = client.post("/internal/finops/query", json=body)
    assert response.status_code == 422
    assert "scope" not in response.text
    body.pop("scope")
    body["window"]["start"] = (NOW - timedelta(days=100)).isoformat()
    assert client.post("/internal/finops/query", json=body).status_code == 422


def test_hosted_changes_persist_immutable_snapshot_and_require_independent_production_approval(
    config, store, tmp_path
):
    changes = CosmosChangeStore(tmp_path, store, USER, PortalApprovalService(store.container))
    changes.workspace = tmp_path
    change = ChangeSet(
        id="cs-20261016-120000-abcdef",
        created_at=NOW,
        created_by=USER,
        environment="prod",
        environment_class="prod",
        mode="pr",
        summary="Preview a change",
        intents=[],
        files={},
        steps=[{"id": "manual", "kind": "prerequisite", "description": "Manual prerequisite"}],
        repo_base_commit="a" * 40,
        admin_policy_blob="b" * 40,
        compat={"agentVersion": "0.1.0", "agentUpstreamCommit": "c" * 40, "repoUpstreamCommit": "c" * 40},
        branch="agent/test",
    )
    changes.save(change, {})
    assert changes.load(change.id) == change
    with pytest.raises(PortalStateConflict):
        changes.save(change, {})
    factory = SimpleNamespace(approvals=PortalApprovalService(store.container))
    service = PortalAssistantService(store, factory)
    assert service.change(change.id).approval_status == "pending"
    with pytest.raises(PermissionError, match="independent"):
        service.approve(
            change.id, ChangeDecision(approval_hash=change.content_hash()), identity(), grant(config)
        )
    with pytest.raises(PortalStateConflict, match="approval"):
        service.execute(
            change.id, ChangeDecision(approval_hash=change.content_hash()), identity(), grant(config)
        )
    calls = []

    def execute(change_id, approval_hash):
        calls.append((change_id, approval_hash))
        changes.record(
            change_id,
            "change.recorded",
            state="awaiting-merge",
            pullRequest={"number": 1, "url": "https://github.com/example/contracts/pull/1"},
        )
        return {"pullRequest": {"number": 1, "url": "https://github.com/example/contracts/pull/1"}}

    @contextmanager
    def workspace():
        yield tmp_path, SimpleNamespace(base_commit=lambda: "a" * 40)

    factory.workspace = workspace
    factory.github_token = "configured-credential-reference"
    factory.create = lambda *_: SimpleNamespace(
        loaded=SimpleNamespace(blob_sha="b" * 40),
        lock=SimpleNamespace(upstream=SimpleNamespace(commit="c" * 40)),
        audit=SimpleNamespace(record=lambda *args, **kwargs: None),
        engine=lambda: SimpleNamespace(execute=execute),
    )
    decision = ChangeDecision(approval_hash=change.content_hash())
    approved = service.approve(change.id, decision, identity(OTHER), grant(config, OTHER))
    assert approved.approval_status == "approved"
    submitted = service.execute(change.id, decision, identity(), grant(config))
    assert submitted.execution_status == "submitted"
    assert submitted.pull_request_url == "https://github.com/example/contracts/pull/1"
    with pytest.raises(PortalStateConflict, match="already submitted"):
        service.execute(change.id, decision, identity(), grant(config))
    assert calls == [(change.id, change.content_hash())]
    approval_id = store.get("hosted-change", change.id).record.data["approvalId"]
    del store.container.items[approval_id]
    assert service.change(change.id).approval_status == "expired"
