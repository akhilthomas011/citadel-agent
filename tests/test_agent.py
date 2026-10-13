# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the agent core: tool registry, guard, drafts, core tools, approvals and instructions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from citadel_agent.agent import build_instructions, build_registry
from citadel_agent.changes.changeset import ChangeSet
from citadel_agent.changes.drafts import DraftError, DraftStore
from citadel_agent.changes.store import ChangeStore
from citadel_agent.cli_chat import describe_approval, parse_arguments
from citadel_agent.contracts import parse_intent
from citadel_agent.knowledge.docs import DocIndex, parse_markdown
from citadel_agent.policy import Operation
from citadel_agent.safety.redaction import REDACTED, default_redactor
from citadel_agent.tools import TOOL_NAMES, core
from citadel_agent.tools.context import ToolContext
from citadel_agent.tools.registry import ToolInputError, ToolRegistry, ToolSpec, guarded


def _intent(environment: str = "dev", use_case: str = "Chat") -> Any:
    return parse_intent(
        {
            "kind": "access-contract",
            "environment": environment,
            "useCase": {"businessUnit": "HR", "useCaseName": use_case, "environment": environment.upper()},
            "services": [{"code": "LLM", "apis": ["universal-llm-api"], "keySecretName": "HR-CHAT-KEY"}],
        }
    )


def _change(**overrides: Any) -> ChangeSet:
    return ChangeSet.model_validate(
        {
            "id": "cs-20261007-120000-abcdef",
            "createdAt": "2026-10-07T12:00:00Z",
            "createdBy": "dev@contoso.com",
            "environment": "dev",
            "environmentClass": "dev",
            "mode": "direct",
            "summary": "Onboard HR chat",
            "intents": ["contracts/access/hr/chat/dev"],
            "files": {},
            "repoBaseCommit": "c",
            "compat": {"agentVersion": "1", "agentUpstreamCommit": "a", "repoUpstreamCommit": "b"},
            "branch": "citadel-agent/access/dev-cs-20261007-120000-abcdef",
            "steps": [{"id": "manual", "kind": "prerequisite", "description": "Grant a role"}],
            **overrides,
        }
    )


# Registry and guard ----------------------------------------------------------------------------


def test_registry_enforces_the_tool_vocabulary_and_personas() -> None:
    registry = ToolRegistry()
    registry.add(ToolSpec("search_docs", lambda: "", "d"))
    with pytest.raises(ValueError, match="vocabulary"):
        registry.add(ToolSpec("delete_everything", lambda: "", "d"))
    with pytest.raises(ValueError, match="twice"):
        registry.add(ToolSpec("search_docs", lambda: "", "d"))
    with pytest.raises(ValueError, match="personas"):
        registry.add(ToolSpec("read_doc", lambda: "", "d", personas=frozenset({"admin"})))
    registry.add(ToolSpec("backend_contract_summary", lambda: "", "d", personas=frozenset({"operator"})))
    assert [s.name for s in registry.for_persona("consumer")] == ["search_docs"]
    assert len(registry.for_persona("operator")) == 2


def test_guard_redacts_output_and_reports_errors() -> None:
    secret = "sk-test-0123456789abcdef0123456789"
    default_redactor().register_secret(secret)

    def leaky() -> str:
        return f"the key is {secret}"

    def failing() -> str:
        raise ToolInputError("the draft is invalid")

    def crashing() -> str:
        raise RuntimeError(f"boom {secret}")

    assert asyncio.run(guarded(leaky)()) == f"the key is {REDACTED}"
    assert asyncio.run(guarded(failing)()) == "Error: the draft is invalid"
    assert asyncio.run(guarded(crashing)()) == f"Error: RuntimeError: boom {REDACTED}"
    assert asyncio.run(guarded(lambda: "x" * 20000)()).endswith("[output truncated]")


def test_core_tools_are_registered_with_approval_only_on_execute() -> None:
    registry = build_registry(ToolContext(runtime=SimpleNamespace(), drafts=DraftStore(Path("."))))
    assert set(registry.specs) <= TOOL_NAMES
    assert {name for name, spec in registry.specs.items() if spec.requires_approval} == {"execute_change"}
    tools = {t.name: t for t in registry.build("consumer")}
    assert tools["execute_change"].approval_mode == "always_require"
    assert tools["search_docs"].parameters()["properties"]["limit"]["maximum"] == 10


# Drafts ----------------------------------------------------------------------------------------


def test_draft_store_round_trip(tmp_path: Path) -> None:
    store = DraftStore(tmp_path)
    draft = store.save(_intent(), operation=Operation.ACCESS_WRITE, created_by="dev@contoso.com")
    assert draft.id.startswith("d-access-hr-chat-dev-") and draft.step_id == "access-chat"
    loaded = store.load(draft.id)
    assert loaded.intent == draft.intent and loaded.operation is Operation.ACCESS_WRITE
    revised = store.save(
        _intent(use_case="Chat"), operation=Operation.ACCESS_WRITE, created_by="x", draft_id=draft.id
    )
    assert [d.id for d in store.list()] == [revised.id]
    store.delete(draft.id)
    assert store.list() == []
    with pytest.raises(DraftError):
        store.load("../escape")
    with pytest.raises(DraftError, match="does not exist"):
        store.load(draft.id)


# Core tools ------------------------------------------------------------------------------------


class FakeEngine:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.executed: list[tuple[str, str]] = []

    def prepare(self, request: Any) -> Any:
        self.requests.append(request)
        change = SimpleNamespace(id="cs-20261007-120000-abcdef")
        return SimpleNamespace(change=change, content_hash="sha256:abc", summary=lambda: "Change summary")

    def execute(self, change_id: str, approval_hash: str) -> dict[str, Any]:
        self.executed.append((change_id, approval_hash))
        return {"change": change_id, "mode": "direct", "recorded": "base-branch"}


def _context(tmp_path: Path) -> ToolContext:
    index = DocIndex(
        parse_markdown("guides/keys.md", "# Keys\n\n## Rotation\n\nRotate the key in three steps.\n")
    )
    runtime = SimpleNamespace(docs=lambda: index, store=ChangeStore(tmp_path / "changes"))
    return ToolContext(runtime=runtime, drafts=DraftStore(tmp_path / "drafts"), _engine=FakeEngine())


def test_documentation_tools(tmp_path: Path) -> None:
    context = _context(tmp_path)
    assert "Citation: guides/keys.md#rotation" in core.search_docs(context, "rotate key")
    assert "three steps" in core.read_doc(context, "guides/keys.md#rotation")
    with pytest.raises(ToolInputError, match="search_docs"):
        core.read_doc(context, "guides/keys.md#missing")


def test_gateway_overview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from citadel_agent.gateway.models import BackendContractView

    contract = BackendContractView(
        {
            "circuitBreaker": {"enabled": True},
            "backends": [{"backendId": "b1", "supportedModels": [{"name": "gpt-5.2"}]}],
            "pools": [{"poolName": "p1"}],
            "modelAliases": [{"name": "chat-default"}],
        }
    )
    monkeypatch.setattr(core, "GatewayClient", lambda url: SimpleNamespace(backend_contract=lambda: contract))
    release = {"master-version": "1.2.0", "access-contract-version": "1.2.0"}
    env = SimpleNamespace(
        environment_class="dev", mode=SimpleNamespace(value="direct"), gateway_url="https://gw.example"
    )
    context = _context(tmp_path)
    context.runtime = SimpleNamespace(
        policy=SimpleNamespace(
            default_environment="dev", environment=lambda name: env, environments={"dev": env}
        ),
        settings=SimpleNamespace(environment=None),
        loaded=SimpleNamespace(error=None),
        lock=SimpleNamespace(upstream=SimpleNamespace(release=release, commit="a" * 40)),
        live_release=lambda e: dict(release),
        can_deploy=lambda e: True,
    )
    output = core.gateway_overview(context)
    assert "## Gateway dev (dev, direct mode)" in output and "Compatibility: ok" in output
    assert "Models: chat-default, gpt-5.2" in output
    assert "Backends: 1, pools: 1, aliases: 1" in output and "Circuit breaker: on" in output


def test_prepare_and_execute_change_with_drafts(tmp_path: Path) -> None:
    context = _context(tmp_path)
    draft = context.drafts.save(_intent(), operation=Operation.ACCESS_WRITE, created_by="dev")
    output = core.prepare_change(context, [draft.id], "Onboard HR chat")
    assert "change_id: cs-20261007-120000-abcdef" in output and "approval_hash: sha256:abc" in output
    request = context.engine().requests[0]
    assert request.environment == "dev" and request.changes[0].step_id == draft.step_id

    result = core.execute_change(context, "cs-20261007-120000-abcdef", "sha256:abc")
    assert "recorded as base-branch" in result and "deployed" in result
    assert context.drafts.list() == []


def test_prepare_change_rejects_mixed_environments_and_unknown_drafts(tmp_path: Path) -> None:
    context = _context(tmp_path)
    first = context.drafts.save(_intent("dev"), operation=Operation.ACCESS_WRITE, created_by="dev")
    second = context.drafts.save(_intent("test", "Bot"), operation=Operation.ACCESS_WRITE, created_by="dev")
    with pytest.raises(ToolInputError, match="same environment"):
        core.prepare_change(context, [first.id, second.id], "Two environments")
    with pytest.raises(ToolInputError, match="does not exist"):
        core.prepare_change(context, ["d-missing-000000"], "Missing draft")


def test_change_status(tmp_path: Path) -> None:
    context = _context(tmp_path)
    assert "No changes" in core.change_status(context)
    change = _change()
    context.runtime.store.save(change, {})
    context.runtime.store.record(change.id, "step.manual", step="manual", status="manual")
    assert change.id in core.change_status(context)
    detail = core.change_status(context, change.id)
    assert "Step manual (manual prerequisite): manual" in detail
    with pytest.raises(ToolInputError):
        core.change_status(context, "cs-missing")


# Approvals and instructions --------------------------------------------------------------------


def test_execute_approval_is_rendered_from_the_stored_change(tmp_path: Path) -> None:
    context = _context(tmp_path)
    change = _change()
    context.runtime.store.save(change, {})
    good = {"change_id": change.id, "approval_hash": change.content_hash()}
    text, refuse = describe_approval(context, "execute_change", good)
    assert not refuse and "Onboard HR chat" in text and "deploy this change now" in text

    text, refuse = describe_approval(context, "execute_change", {**good, "approval_hash": "sha256:other"})
    assert refuse and "does not match" in text
    assert describe_approval(context, "execute_change", {"change_id": "cs-unknown"})[1]
    assert describe_approval(context, "other_tool", {"a": 1}) == ('other_tool({"a": 1})', False)


def test_parse_arguments() -> None:
    assert parse_arguments('{"change_id": "cs-1"}') == {"change_id": "cs-1"}
    assert parse_arguments({"a": 1}) == {"a": 1}
    assert parse_arguments(None) == {}
    assert parse_arguments("not json") == {"arguments": "not json"}


class _Stream:
    def __init__(self, text: str, requests: list[Any]) -> None:
        self._updates = [SimpleNamespace(text=text)]
        self._final = SimpleNamespace(user_input_requests=requests)

    def __aiter__(self) -> Any:
        async def generate() -> Any:
            for update in self._updates:
                yield update

        return generate()

    async def get_final_response(self) -> Any:
        return self._final


class _Request:
    def __init__(self, name: str) -> None:
        self.function_call = SimpleNamespace(name=name, arguments="{}")

    def to_function_approval_response(self, approved: bool) -> str:
        return f"{self.function_call.name}:{approved}"


def test_run_turn_resolves_approval_requests(capsys: pytest.CaptureFixture[str]) -> None:
    from citadel_agent.cli_chat import run_turn

    sent: list[Any] = []
    streams = [_Stream("Preparing. ", [_Request("execute_change")]), _Stream("Deployed.", [])]

    class FakeAgent:
        def run(self, message: Any, *, session: Any, stream: bool) -> _Stream:
            sent.append(message)
            return streams[len(sent) - 1]

    asyncio.run(run_turn(FakeAgent(), "session", "deploy it", lambda request: True))
    assert sent[0] == "deploy it"
    assert sent[1].role == "user" and sent[1].contents[0].text == "execute_change:True"
    assert "Preparing. " in capsys.readouterr().out


def _runtime(*, policy: Any = None, persona: str = "consumer") -> SimpleNamespace:
    return SimpleNamespace(
        principal=SimpleNamespace(label="dev@contoso.com"),
        caller=SimpleNamespace(persona=SimpleNamespace(value=persona)),
        policy=policy,
        loaded=SimpleNamespace(error="no repository configured"),
        settings=SimpleNamespace(environment=None),
    )


def test_instructions_reflect_policy_and_persona() -> None:
    read_only = build_instructions(_runtime())
    assert "read-only" in read_only and "no repository configured" in read_only
    assert "platform team" in read_only and "Never display, request or accept keys" in read_only

    env = SimpleNamespace(environment_class="dev", mode=SimpleNamespace(value="direct"))
    policy = SimpleNamespace(default_environment="dev", environments={"dev": env})
    operator = build_instructions(_runtime(policy=policy, persona="operator"))
    assert "dev (dev, direct mode)" in operator and "current environment is dev" in operator
    assert "platform team" not in operator
