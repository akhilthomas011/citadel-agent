# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tool routing, usage logging and agent composition."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from citadel_agent.agent import build_registry
from citadel_agent.tool_routing import (
    CORE_TOOLS,
    DOMAIN_TOOLS,
    ToolRouter,
    classify,
    next_recent,
    select_domains,
    unrouted,
)
from citadel_agent.tools import TOOL_NAMES
from citadel_agent.usage_log import TurnUsage, UsageLog, summarize
from tool_fakes import FakeContext, make_intent, make_record


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("What is Citadel and how does it work?", set()),
        ("Which models can my team use?", set()),
        ("Show me the HR chatbot contract", set()),
        ("Onboard the HR chatbot in dev with PII masking", {"access"}),
        ("Raise the TPM of Finance Forecast to 20000", {"access"}),
        ("Add gpt-5.4-mini to the allowed models", {"access"}),
        ("Why do I get a 429 from the gateway?", {"diagnostics"}),
        ("Show usage and cost for Finance Forecast last week", {"usage"}),
        ("Rotate our subscription key", {"rotation"}),
        ("Add the Foundry account to the backend with priority 2", {"backend"}),
        ("Email us when requests are throttled", {"alerts", "diagnostics"}),
        ("Publish our MCP server through the gateway", {"publish"}),
        ("Give me a Python snippet for gpt-5.2", {"calling"}),
    ],
)
def test_messages_are_classified_by_task(message: str, expected: set[str]) -> None:
    assert expected <= classify(message)
    if not expected:
        assert classify(message) == set()


def test_error_questions_do_not_pull_in_drafting_or_code_tools() -> None:
    assert classify("Why would I get a 429 with code quota_exceeded?") == {"diagnostics"}


def test_every_tool_is_core_or_in_a_group() -> None:
    assert unrouted(TOOL_NAMES) == set()
    assert CORE_TOOLS <= TOOL_NAMES
    assert set().union(*DOMAIN_TOOLS.values()) <= TOOL_NAMES


def test_a_change_never_loses_its_approval_tools() -> None:
    assert {"prepare_change", "execute_change", "change_status"} <= CORE_TOOLS


def test_follow_ups_keep_the_topic_and_unrelated_questions_let_it_age_out() -> None:
    recent = next_recent([], "Rotate our key", {"rotation"})
    assert recent == [["rotation"]]

    domains, matched = select_domains("yes, go ahead", recent)
    assert domains == {"rotation"} and matched == set()
    assert next_recent(recent, "yes, go ahead", matched) == [["rotation"]]

    changed = next_recent(recent, "Tell me about the architecture of the whole gateway", set())
    changed = next_recent(changed, "And how does the documentation describe the policies", set())
    assert select_domains("hello there everyone", changed)[0] == set()


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeSessionContext:
    def __init__(self, text: str) -> None:
        self.input_messages = [SimpleNamespace(role="user", text=text)]
        self.added: list[str] = []

    def extend_tools(self, source_id: str, tools: list[Any]) -> None:
        self.added.extend(t.name for t in tools)


def run_router(router: ToolRouter, state: dict[str, Any], text: str) -> list[str]:
    context = FakeSessionContext(text)
    asyncio.run(router.before_run(agent=None, session=None, context=context, state=state))
    return context.added


def test_router_adds_only_the_requested_groups_and_remembers_them() -> None:
    router = ToolRouter([FakeTool(n) for n in sorted(TOOL_NAMES)])
    state: dict[str, Any] = {}

    assert run_router(router, state, "What is Citadel?") == []
    first = run_router(router, state, "Why is my call failing with a 403?")
    assert set(first) == DOMAIN_TOOLS["diagnostics"]
    follow_up = run_router(router, state, "ok")
    assert set(follow_up) == DOMAIN_TOOLS["diagnostics"]
    assert {t.name for t in router.core} == CORE_TOOLS


def test_router_state_is_json_serializable() -> None:
    router = ToolRouter([FakeTool(n) for n in sorted(TOOL_NAMES)])
    state: dict[str, Any] = {}
    run_router(router, state, "Onboard a new use case")

    assert json.loads(json.dumps(state)) == state


def test_routing_cuts_the_tool_schema_sent_to_the_model(tmp_path: Path) -> None:
    context = FakeContext(tmp_path, [make_record(make_intent())], persona="operator")
    tools = build_registry(context).build("operator")

    def size(selected: list[Any]) -> int:
        return sum(len(json.dumps(t.to_json_schema_spec())) for t in selected)

    router = ToolRouter(tools)
    everything = size(tools)
    question = size(router.core)
    diagnosing = size(router.core + [t for t in tools if t.name in DOMAIN_TOOLS["diagnostics"]])

    assert question < everything * 0.5
    assert diagnosing < everything * 0.75


def test_turn_usage_collects_one_entry_per_model_request() -> None:
    turn = TurnUsage()
    request = {"input_token_count": 4000, "output_token_count": 50, "cache_read_input_token_count": 1000}
    for details in (request, {"input_token_count": 5200, "output_token_count": 120}):
        turn.add_update(SimpleNamespace(contents=[SimpleNamespace(type="usage", usage_details=details)]))
    turn.add_update(SimpleNamespace(contents=[SimpleNamespace(type="text", usage_details=None)]))

    assert [r["input"] for r in turn.requests] == [4000, 5200]
    assert turn.input_tokens == 9200 and turn.output_tokens == 170
    assert "2 model calls" in turn.summary() and "largest request 5,200 in" in turn.summary()


def test_usage_command_summarizes_the_local_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from citadel_agent.cli import app

    log = UsageLog(tmp_path / "usage.jsonl")
    turn = TurnUsage()
    for tokens in (3000, 4000, 9000):
        turn.add_details({"input_token_count": tokens, "output_token_count": 10})
    log.record(turn, session_id="s1", tools=["a"], domains=[])
    monkeypatch.setenv("CITADEL_AGENT_STATE_DIR", str(tmp_path))

    result = CliRunner().invoke(app, ["usage"])
    assert result.exit_code == 0, result.output
    assert "9,000" in result.output and "Model requests" in result.output

    recent = CliRunner().invoke(app, ["usage", "--last", "1"])
    assert recent.exit_code == 0 and "9,000" in recent.output and "3,000" not in recent.output
    assert summarize(log.path, last=2)["requests"] == 2

    monkeypatch.setenv("CITADEL_AGENT_STATE_DIR", str(tmp_path / "empty"))
    assert "No usage recorded yet" in CliRunner().invoke(app, ["usage"]).output


def test_usage_log_records_counts_only_and_summarizes(tmp_path: Path) -> None:
    log = UsageLog(tmp_path / "state" / "usage.jsonl")
    turn = TurnUsage()
    for tokens in (3000, 4000, 9000):
        turn.add_details({"input_token_count": tokens, "output_token_count": 10})

    log.record(turn, session_id="s1", tools=["a", "b"], domains=["access"], history_messages=6)
    log.record(TurnUsage(), session_id="s1", tools=[], domains=[])

    rows = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert [r["input"] for r in rows] == [3000, 4000, 9000]
    assert set(rows[0]) == {
        "ts",
        "session",
        "call",
        "input",
        "output",
        "cached",
        "tools",
        "domains",
        "history",
    }
    assert summarize(log.path)["input_max"] == 9000
    assert summarize(tmp_path / "missing.jsonl") == {}
