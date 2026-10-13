# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Harness for agent evaluations: the real agent and a live model, with every tool stubbed.

The agent is composed exactly as in production (instructions, skills, tool routing, approval
flags), but each tool is replaced by a recording stub that returns a canned answer, so no scenario
can read or change Azure. Scenarios then assert on which tools the model called and on its reply.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from citadel_agent.agent import build_registry, create_agent
from citadel_agent.changes.drafts import DraftStore
from citadel_agent.cli_chat import parse_arguments, run_turn
from citadel_agent.policy.authz import Caller, Persona
from citadel_agent.tools.context import ToolContext
from citadel_agent.tools.registry import ToolRegistry, ToolSpec
from citadel_agent.usage_log import TurnUsage

DEFAULT_ANSWER = "(stub) done."
READ_ONLY_PREFIXES = ("list_", "describe_", "search_", "read_", "explain_", "gateway_", "usage_", "backend_")


@dataclass
class Call:
    name: str
    arguments: dict[str, Any]


@dataclass
class Outcome:
    """Everything observable about one scripted conversation."""

    calls: list[Call] = field(default_factory=list)
    approvals: list[Call] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)
    usage: TurnUsage = field(default_factory=TurnUsage)

    @property
    def text(self) -> str:
        return "\n".join(self.replies)

    def called(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.name == name]

    def names(self) -> list[str]:
        return [c.name for c in self.calls]


class PersonaRuntime:
    """The real runtime with the caller's persona replaced, so one eval can play a consumer."""

    def __init__(self, base: Any, persona: str | None) -> None:
        self._base = base
        self._persona = persona

    def __getattr__(self, name: str) -> Any:
        if name == "caller" and self._persona is not None:
            return Caller(self._base.caller.principal, Persona(self._persona))
        return getattr(self._base, name)


def stub_registry(
    real: ToolRegistry, outcome: Outcome, canned: Mapping[str, str | Callable[[dict[str, Any]], str]]
) -> ToolRegistry:
    """A registry with the real tool names, descriptions, schemas and approval flags, but stub bodies."""
    stubbed = ToolRegistry()
    for spec in real.specs.values():
        stubbed.add(
            ToolSpec(
                spec.name,
                _stub(spec, outcome, canned),
                spec.description,
                spec.personas,
                spec.requires_approval,
            )
        )
    return stubbed


def _stub(spec: ToolSpec, outcome: Outcome, canned: Mapping[str, Any]) -> Callable[..., str]:
    @functools.wraps(spec.function)
    def stub(*args: Any, **kwargs: Any) -> str:
        outcome.calls.append(Call(spec.name, dict(kwargs)))
        answer = canned.get(spec.name, DEFAULT_ANSWER)
        return answer(kwargs) if callable(answer) else answer

    stub.__signature__ = inspect.signature(spec.function)  # type: ignore[attr-defined]
    return stub


def registered_tools(runtime: Any) -> set[str]:
    """Names of the tools the agent actually registers (some may be absent in a given build)."""
    state = runtime.store.directory.parent
    return set(build_registry(ToolContext(runtime=runtime, drafts=DraftStore(state / "drafts"))).specs)


def run_conversation(
    runtime: Any,
    messages: list[str],
    *,
    persona: str | None = None,
    canned: Mapping[str, Any] | None = None,
    approve: Callable[[Call], bool] = lambda call: False,
) -> Outcome:
    """Send ``messages`` one after another to a fresh agent session and record what happened."""
    outcome = Outcome()
    playing = PersonaRuntime(runtime, persona)
    state = playing.store.directory.parent
    real = build_registry(ToolContext(runtime=playing, drafts=DraftStore(state / "drafts")))
    bundle = create_agent(playing, registry=stub_registry(real, outcome, canned or {}))

    def decide(request: Any) -> bool:
        call = getattr(request, "function_call", None)
        record = Call(getattr(call, "name", "?"), parse_arguments(getattr(call, "arguments", None)))
        outcome.approvals.append(record)
        return approve(record)

    async def main() -> None:
        session = bundle.agent.create_session()
        for message in messages:
            usage = await run_turn(bundle.agent, session, message, decide)
            outcome.usage.requests.extend(usage.requests)
            outcome.replies.append(_last_reply(session))

    asyncio.run(main())
    return outcome


def _last_reply(session: Any) -> str:
    history = session.state.get("in_memory", {}).get("messages", [])
    for message in reversed(history):
        if str(getattr(message, "role", "")).lower() == "assistant" and getattr(message, "text", ""):
            return message.text
    return ""
