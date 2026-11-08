"""Durable assistant acceptance fences workers and never replays started/interrupted effects."""

import copy
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, FastAPI

from citadel_agent.portal import worker as worker_module
from citadel_agent.portal.assistant import PortalAssistantService
from citadel_agent.portal.authz import PortalAuthorizationError
from citadel_agent.portal.grants import VersionedHubGrant
from citadel_agent.portal.product_contracts import TurnRequest
from citadel_agent.portal.runtime import PortalRuntimeSettings
from citadel_agent.portal.sessions import PortalSessionRecord, PortalStateConflict
from citadel_agent.portal.tools import hosted_registry
from citadel_agent.tools.registry import ToolRegistry, ToolSpec
from test_portal_product import NOW, USER, grant, identity
from test_portal_product import config as config_fixture
from test_portal_product import store as store_fixture

config = config_fixture
store = store_fixture
REFERENCE = "a" * 64


def setup(config, store, runner):
    session = PortalSessionRecord(
        id=REFERENCE,
        tenant_id=config.scope.tenant_id,
        subject_id=USER,
        client_id="portal",
        roles=identity().roles,
        scopes=frozenset({"access_as_user"}),
        csrf_token="c" * 43,
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        ttl=3600,
    )
    sessions = SimpleNamespace(
        get_session=lambda ref, now: session if ref == REFERENCE and session.expires_at > now else None
    )
    grants = SimpleNamespace(get=lambda *args: VersionedHubGrant(grant(config), "grant"))
    service = PortalAssistantService(
        store,
        SimpleNamespace(),
        runner=runner,
        durable_turns=True,
        worker_sessions=sessions,
        worker_grants=grants,
    )
    service.claim_worker("worker-one")
    return service, sessions, grants


@pytest.mark.asyncio
async def test_acceptance_is_durable_without_replica_background_task_and_runs_once(config, store):
    calls = []

    async def runner(data, message, user, user_grant):
        calls.append(message)
        return "Done", {}, {}

    service, _, _ = setup(config, store, runner)
    conversation = service.create_conversation(identity())
    request = TurnRequest(request_id=uuid4(), message="Inspect current gateway")
    tasks = BackgroundTasks()
    accepted = await service.turn(
        conversation.id,
        request,
        identity(),
        grant(config),
        session_reference=REFERENCE,
        background_tasks=tasks,
    )
    assert accepted.status == "queued" and not tasks.tasks and not calls
    assert await service.process_queued_turn("worker-one")
    assert calls == ["Inspect current gateway"]
    assert service.conversation(conversation.id, identity()).status == "idle"
    assert (
        await service.turn(conversation.id, request, identity(), grant(config), session_reference=REFERENCE)
    ).status == "idle"
    assert not await service.process_queued_turn("worker-one")


@pytest.mark.asyncio
async def test_session_and_grant_are_rechecked_before_processing(config, store):
    async def runner(*args):
        pytest.fail("revoked work must not invoke a model")

    service, _, grants = setup(config, store, runner)
    conversation = service.create_conversation(identity())
    await service.turn(
        conversation.id,
        TurnRequest(request_id=uuid4(), message="Inspect"),
        identity(),
        grant(config),
        session_reference=REFERENCE,
    )
    grants.get = lambda *args: None
    assert await service.process_queued_turn("worker-one")
    result = service.conversation(conversation.id, identity())
    assert result.status == "failed"
    assert result.error_code == "queued-turn-authorization-or-request-invalid"


@pytest.mark.asyncio
async def test_cancelled_turn_cannot_be_overwritten_by_stale_worker(config, store):
    service = None
    conversation = None

    async def runner(*args):
        service.cancel_turn(conversation.id, identity())
        return "Must not overwrite", {}, {}

    service, _, _ = setup(config, store, runner)
    conversation = service.create_conversation(identity())
    await service.turn(
        conversation.id,
        TurnRequest(request_id=uuid4(), message="Inspect"),
        identity(),
        grant(config),
        session_reference=REFERENCE,
    )
    await service.process_queued_turn("worker-one")
    result = service.conversation(conversation.id, identity())
    assert result.status == "failed" and result.error_code == "turn-cancelled"
    assert not any(message.content == "Must not overwrite" for message in result.messages)


@pytest.mark.asyncio
async def test_started_expired_turn_is_not_replayed(config, store):
    calls = []

    async def runner(*args):
        calls.append("model")
        return "Done", {}, {}

    service, _, _ = setup(config, store, runner)
    conversation = service.create_conversation(identity())
    await service.turn(
        conversation.id,
        TurnRequest(request_id=uuid4(), message="Inspect"),
        identity(),
        grant(config),
        session_reference=REFERENCE,
    )
    current = store.get("conversation", conversation.id)
    data = copy.deepcopy(current.record.data)
    data.update(status="running", executionStarted=True, leaseUntil=(NOW - timedelta(minutes=1)).isoformat())
    store.replace(current, data)
    await service.process_queued_turn("worker-one")
    assert not calls
    assert service.conversation(conversation.id, identity()).error_code == "turn-interrupted-review-drafts"


def test_global_worker_lease_prevents_parallel_drain_and_stale_release(config, store):
    service, _, _ = setup(config, store, SimpleNamespace())
    assert not service.claim_worker("worker-two")
    with pytest.raises(PortalStateConflict):
        service.renew_worker("worker-two")
    service.renew_worker("worker-one", release=True)
    assert service.claim_worker("worker-two")


@pytest.mark.asyncio
async def test_explicit_disabled_grant_blocks_worker_even_when_it_exists(config, store):
    async def runner(*args):
        pytest.fail("disabled grants must prevent execution")

    service, _, grants = setup(config, store, runner)
    conversation = service.create_conversation(identity())
    await service.turn(
        conversation.id,
        TurnRequest(request_id=uuid4(), message="Inspect"),
        identity(),
        grant(config),
        session_reference=REFERENCE,
    )
    disabled = grant(config).model_copy(update={"enabled": False})
    grants.get = lambda *args: VersionedHubGrant(disabled, "disabled")
    await service.process_queued_turn("worker-one")
    assert service.conversation(conversation.id, identity()).status == "failed"


@pytest.mark.asyncio
async def test_expired_unstarted_claim_recovers_before_exactly_one_execution(config, store):
    calls = []

    async def runner(*args):
        calls.append("model")
        return "Done", {}, {}

    service, _, _ = setup(config, store, runner)
    conversation = service.create_conversation(identity())
    await service.turn(
        conversation.id,
        TurnRequest(request_id=uuid4(), message="Inspect"),
        identity(),
        grant(config),
        session_reference=REFERENCE,
    )
    current = store.get("conversation", conversation.id)
    data = copy.deepcopy(current.record.data)
    data.update(status="running", executionStarted=False, leaseUntil=(NOW - timedelta(minutes=1)).isoformat())
    store.replace(current, data)
    assert await service.process_queued_turn("worker-one")
    assert not calls and service.conversation(conversation.id, identity()).status == "queued"
    assert await service.process_queued_turn("worker-one")
    assert calls == ["model"]
    assert not await service.process_queued_turn("worker-one")


def test_expired_worker_cannot_renew_or_release_and_requires_a_new_claim(config, store):
    service, _, _ = setup(config, store, SimpleNamespace())
    current = store.get("worker-heartbeat", "assistant")
    store.replace(current, {**current.record.data, "leaseUntil": (NOW - timedelta(seconds=1)).isoformat()})
    for release in (False, True):
        with pytest.raises(PortalStateConflict, match="expired"):
            service.renew_worker("worker-one", release=release)
    assert service.claim_worker("worker-two")


def test_tool_guard_rechecks_current_grant_between_invocations(config):
    calls = []
    current_grant = grant(config)
    registry = ToolRegistry()
    registry.add(ToolSpec("search_docs", lambda: calls.append("tool"), "test function"))
    selected = hosted_registry(
        registry,
        identity(),
        current_grant,
        data=None,
        audit=SimpleNamespace(record=lambda *args, **kwargs: None),
        refresh_authorization=lambda: (identity(), current_grant),
    )
    selected.specs["search_docs"].function()
    current_grant = current_grant.model_copy(update={"enabled": False})
    with pytest.raises(PortalAuthorizationError):
        selected.specs["search_docs"].function()
    assert calls == ["tool"]


@pytest.mark.asyncio
async def test_finite_worker_uses_app_lifespan_and_releases_ownership(monkeypatch):
    events = []

    @asynccontextmanager
    async def lifespan(app):
        events.append("start")
        yield
        events.append("stop")

    async def drain(owner):
        events.append("drain")
        return False

    def renew(owner, *, release=False):
        events.append("release" if release else "heartbeat")

    app = FastAPI(lifespan=lifespan)
    app.state.portal_product = SimpleNamespace(
        assistant=SimpleNamespace(
            claim_worker=lambda owner: events.append("claim") or True,
            process_queued_turn=drain,
            renew_worker=renew,
        )
    )
    monkeypatch.setattr(worker_module, "create_agent_app_from_settings", lambda settings: app)
    settings = PortalRuntimeSettings.model_construct(
        durable_turns_enabled=True, assistant_worker_max_seconds=10
    )
    assert await worker_module.run_assistant_worker(settings) == 0
    assert events == ["start", "claim", "drain", "release", "stop"]
