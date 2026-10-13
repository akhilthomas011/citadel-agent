# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Per-turn tool selection: send the model only the tools a request is likely to need.

Every tool's schema is part of every model request, and the operator toolset is large. The
router keeps a small always-on core and adds the tool groups (domains) the user's message
mentions, plus the groups from the last turns so follow-ups such as "yes, go ahead" keep working.
A message that names no group gets the core only, which is enough to answer questions and to
review or approve a prepared change; the agent's instructions tell the user how to ask for more.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_framework import ContextProvider

SOURCE_ID = "tool-router"
STICKY_TURNS = 2
CONTINUATION_WORDS = 4

# Always available: orientation, documentation, and the change workflow's last steps.
CORE_TOOLS: frozenset[str] = frozenset(
    {
        "search_docs",
        "read_doc",
        "gateway_overview",
        "list_models",
        "list_access_contracts",
        "describe_access_contract",
        "prepare_change",
        "execute_change",
        "change_status",
    }
)

DOMAIN_TOOLS: dict[str, frozenset[str]] = {
    "access": frozenset({"draft_access_contract"}),
    "calling": frozenset({"generate_client_code", "smoke_test"}),
    "diagnostics": frozenset({"explain_error", "gateway_logs", "smoke_test", "preflight"}),
    "usage": frozenset({"usage_report", "backend_contract_summary"}),
    "rotation": frozenset({"start_key_rotation", "advance_key_rotation"}),
    "backend": frozenset({"draft_backend_change", "backend_contract_summary"}),
    "alerts": frozenset({"draft_alert_rules"}),
    "publish": frozenset({"draft_publish_contract"}),
}

_KEYWORDS: dict[str, str] = {
    "access": (
        r"onboard|new (?:use case|team|app)|\btpm\b|tokens? per minute|\bpii\b|content safety|"
        r"allowed models?|\bjwt\b|monthly quota|token quota|foundry (?:project|agent)|"
        r"(?:change|update|modify|edit|add|remove|raise|lower|increase|decrease|set)\b[^.?!]{0,40}"
        r"\b(?:contract|limit|quota|model|owner|policy)"
    ),
    "calling": (
        r"snippet|sample code|example code|code (?:for|to call|snippet|sample)|\bsdk\b|python|javascript|"
        r"typescript|\bcurl\b|powershell|langchain|semantic kernel|agent framework|"
        r"how (?:do|can) i (?:use|call|connect)|\bsmoke\b|test (?:my|the|our)"
    ),
    "diagnostics": (
        r"error|\b[45]\d\d\b|fail|why|not working|doesn.t work|throttl|denied|unauthori[sz]ed|forbidden|"
        r"\blogs?\b|request.?id|activity.?id|preflight|permission|can.?t|cannot|blocked|timeout|"
        r"broken|issue|problem|wrong"
    ),
    "usage": (
        r"usage|cost|spend|spent|how much|tokens? (?:used|consumed)|report|last (?:week|month|\d+ days?)|"
        r"consum|capacity|backend contract|version|drift|compat"
    ),
    "rotation": r"rotat|regenerat|new key|leak|compromis|expire|secondary key|primary key",
    "backend": (
        r"backend|foundry account|openai account|deployment|alias|pool|circuit|priority|weight|"
        r"session affinity|register (?:a )?model|remove (?:a )?model|onboard (?:a )?model"
    ),
    "alerts": r"alert|notif|e-?mail (?:me|us)|page me|monitor",
    "publish": r"publish|\bmcp\b|\ba2a\b|agent card|tool server|remote mcp|api center",
}
_PATTERNS = {domain: re.compile(pattern, re.IGNORECASE) for domain, pattern in _KEYWORDS.items()}


@dataclass(frozen=True)
class RouteDecision:
    """Which tool groups a turn exposes."""

    domains: tuple[str, ...]
    tool_names: tuple[str, ...]
    matched: tuple[str, ...] = field(default=())


def classify(text: str) -> set[str]:
    """Tool groups whose keywords appear in the message."""
    return {domain for domain, pattern in _PATTERNS.items() if pattern.search(text)}


def is_continuation(text: str) -> bool:
    """A short reply ("yes", "go ahead", an approval) that continues the previous topic."""
    return len(text.split()) <= CONTINUATION_WORDS


def select_domains(text: str, recent: Sequence[Iterable[str]]) -> tuple[set[str], set[str]]:
    """Return ``(domains, matched)``: the groups to expose and those the message itself named."""
    matched = classify(text)
    carried = {domain for turn in recent[-STICKY_TURNS:] for domain in turn}
    return matched | carried, matched


def next_recent(recent: Sequence[Iterable[str]], text: str, matched: Iterable[str]) -> list[list[str]]:
    """The remembered topics after this turn.

    A continuation without keywords changes nothing, so the topic survives "yes" and approvals; a
    new message that names no group counts as a topic change and lets older groups age out.
    """
    history = [sorted(turn) for turn in recent]
    named = sorted(matched)
    if named:
        history.append(named)
    elif not is_continuation(text):
        history.append([])
    return history[-STICKY_TURNS:]


def tool_names_for(domains: Iterable[str]) -> set[str]:
    names: set[str] = set(CORE_TOOLS)
    for domain in domains:
        names |= DOMAIN_TOOLS.get(domain, frozenset())
    return names


def unrouted(tool_names: Iterable[str]) -> set[str]:
    """Tools that belong to neither the core nor any group (a gap the tests guard against)."""
    known = set(CORE_TOOLS).union(*DOMAIN_TOOLS.values())
    return set(tool_names) - known


def _user_text(messages: Sequence[Any]) -> str:
    return " ".join(
        (getattr(message, "text", "") or "")
        for message in messages
        if str(getattr(message, "role", "")).lower() == "user"
    ).strip()


class ToolRouter(ContextProvider):
    """Adds the tools a turn needs; the agent itself is created with the core tools only."""

    def __init__(self, tools: Sequence[Any]) -> None:
        super().__init__(SOURCE_ID)
        self._tools = {tool.name: tool for tool in tools}
        self.last: RouteDecision | None = None

    @property
    def core(self) -> list[Any]:
        return [tool for name, tool in self._tools.items() if name in CORE_TOOLS]

    def decide(self, text: str, recent: Sequence[Iterable[str]]) -> RouteDecision:
        domains, matched = select_domains(text, recent)
        wanted = tool_names_for(domains) - CORE_TOOLS
        names = tuple(name for name in self._tools if name in wanted)
        return RouteDecision(tuple(sorted(domains)), names, tuple(sorted(matched)))

    async def before_run(self, *, agent: Any, session: Any, context: Any, state: dict[str, Any]) -> None:
        recent = [list(turn) for turn in state.get("recent", [])]
        text = _user_text(context.input_messages)
        decision = self.decide(text, recent)
        self.last = decision
        state["recent"] = next_recent(recent, text, decision.matched)
        context.extend_tools(SOURCE_ID, [self._tools[name] for name in decision.tool_names])
