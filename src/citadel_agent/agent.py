# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""The Citadel Agent: Microsoft Agent Framework agent with persona-aware tools and skills."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from citadel_agent.changes.drafts import DraftStore
from citadel_agent.knowledge.catalogs import load_catalogs
from citadel_agent.skills import build_skills, build_skills_provider, load_playbooks, playbooks_for
from citadel_agent.tool_routing import ToolRouter
from citadel_agent.tools.access import register_access_tools
from citadel_agent.tools.alerts import register_alert_tools
from citadel_agent.tools.backend import register_backend_tools
from citadel_agent.tools.context import ToolContext
from citadel_agent.tools.core import register_core_tools
from citadel_agent.tools.diagnostics import register_diagnostic_tools
from citadel_agent.tools.discovery import register_discovery_tools
from citadel_agent.tools.operations import register_operations_tools
from citadel_agent.tools.publish import register_publish_tools
from citadel_agent.tools.registry import ToolRegistry
from citadel_agent.tools.rotation import register_rotation_tools

AGENT_NAME = "CitadelAgent"
AGENT_DESCRIPTION = "Helps teams use and operate the Citadel Governance Hub AI gateway."
# History is compacted once it nears this budget (tool results first, then the oldest turns).
COMPACTION_WINDOW_TOKENS = 16_000
COMPACTION_OUTPUT_TOKENS = 2_000

GUIDELINES = """\
How to work:
* Explain in plain language. Assume the user does not know Azure API Management; briefly define
  terms such as product, subscription key or policy when you first use them.
* Load the matching skill before multi-step tasks (onboarding, troubleshooting, key rotation,
  publishing, alerts) and follow its workflow.
* Ground every factual answer in tool results or upstream documentation, and cite the
  documentation you used with its link. Do not invent model names, limits, product names or URLs.
* Ask short clarifying questions, one or two at a time, when the environment, use case, model or
  limits are unclear.
* Keep answers concise: short paragraphs, bullet lists and small tables.
* Tools for drafting changes, diagnosing errors and reporting usage load when the user's message
  names the task. If you lack a tool the user needs, ask them to say what they want to do, for
  example onboard a use case, rotate a key, publish a tool, create an alert, show usage, or why a
  call fails.

Changes:
* Record requested changes with the draft tools, then call prepare_change. Explain the summary and
  the what-if result in plain words, including changes marked as expected what-if noise.
* Call execute_change only after the user clearly agrees in this conversation. The user also
  confirms in a separate approval prompt that shows the stored change, so never describe a change
  differently from the prepared summary.
* Production environments always use pull requests. If a change is refused by the admin policy,
  explain the reason and who can help; never try to work around it.

Secrets:
* Never display, request or accept keys, tokens, passwords or connection strings. Keys live in
  Key Vault; give the secret name and code that reads it.
* If a user pastes a secret, do not repeat it, and tell them to rotate it."""


@dataclass
class AgentBundle:
    agent: Any
    context: ToolContext
    registry: ToolRegistry
    router: ToolRouter | None = None


def build_instructions(runtime: Any) -> str:
    """System instructions for the caller: identity, persona, environments and guidelines."""
    lines = [
        "You are Citadel Agent, an assistant for the Citadel Governance Hub: an AI gateway on Azure API "
        "Management that governs access to models, MCP tools and agents through versioned contracts.",
        f"The user is {runtime.principal.label}. Their persona is {runtime.caller.persona.value}, derived "
        "from their Azure permissions.",
    ]
    policy = runtime.policy
    if policy is None:
        lines.append(
            f"The admin policy could not be loaded ({runtime.loaded.error}), so you are read-only: you can "
            "answer questions and diagnose problems but not prepare or execute changes."
        )
    else:
        current = runtime.settings.environment or policy.default_environment
        environments = ", ".join(
            f"{name} ({env.environment_class}, {env.mode.value} mode)"
            for name, env in policy.environments.items()
        )
        lines.append(f"Environments: {environments}. The current environment is {current}.")
    if runtime.caller.persona.value != "operator":
        lines.append(
            "Backend onboarding, publishing MCP tools or agents, and gateway-wide alerts need the platform "
            "team; explain this and help the user prepare the request instead."
        )
    return "\n".join(lines) + "\n\n" + GUIDELINES


def build_registry(context: ToolContext) -> ToolRegistry:
    registry = ToolRegistry()
    register_core_tools(registry, context)
    register_discovery_tools(registry, context)
    register_access_tools(registry, context)
    register_diagnostic_tools(registry, context)
    register_operations_tools(registry, context)
    register_rotation_tools(registry, context)
    register_backend_tools(registry, context)
    register_alert_tools(registry, context)
    register_publish_tools(registry, context)
    return registry


def create_agent(runtime: Any, *, client: Any = None, registry: ToolRegistry | None = None) -> AgentBundle:
    """Create the agent for the runtime's caller (tools and skills filtered by persona)."""
    from agent_framework import (
        Agent,
        CompactionProvider,
        ContextWindowCompactionStrategy,
        InMemoryHistoryProvider,
    )

    from citadel_agent.llm_client import create_chat_client

    state = runtime.store.directory.parent
    context = ToolContext(runtime=runtime, drafts=DraftStore(state / "drafts"))
    registry = registry or build_registry(context)
    persona = runtime.caller.persona.value
    playbooks = playbooks_for(load_playbooks(runtime.project_root / "skills"), persona)
    skills = build_skills(playbooks, runtime.docs(), load_catalogs())
    router = ToolRouter(registry.build(persona))
    # The history provider must run first so compaction sees the loaded history. Compaction only acts
    # when the conversation outgrows the budget: it collapses old tool results, then drops old turns.
    history = InMemoryHistoryProvider(skip_excluded=True)
    compaction = CompactionProvider(
        before_strategy=ContextWindowCompactionStrategy(
            max_context_window_tokens=COMPACTION_WINDOW_TOKENS, max_output_tokens=COMPACTION_OUTPUT_TOKENS
        ),
        history_source_id=history.source_id,
    )
    agent = Agent(
        client=client or create_chat_client(runtime),
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        instructions=build_instructions(runtime),
        tools=router.core,
        context_providers=[history, build_skills_provider(skills), router, compaction],
    )
    return AgentBundle(agent=agent, context=context, registry=registry, router=router)
