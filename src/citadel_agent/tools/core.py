# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Core tools: documentation, gateway overview and the prepare/execute change flow.

Implementation functions take the :class:`ToolContext` and are unit-tested directly.
``register_core_tools`` wraps them in closures with plain annotations, which Agent Framework turns
into tool schemas. (This module deliberately does not use postponed annotations.)
"""

from typing import Annotated

from pydantic import Field

from citadel_agent.changes.changeset import StepKind
from citadel_agent.changes.drafts import DraftError
from citadel_agent.changes.engine import ChangeError, ChangeRequest, IntentChange, PrerequisiteStep
from citadel_agent.changes.store import ChangeStoreError
from citadel_agent.gateway.client import GatewayClient
from citadel_agent.tools.context import ToolContext
from citadel_agent.tools.registry import ToolInputError, ToolRegistry, ToolSpec
from citadel_agent.upstream.compat import assess_release

Query = Annotated[str, Field(description="What to look for, in plain words or exact names.")]
Limit = Annotated[int, Field(ge=1, le=10, description="Maximum number of sections.")]
Citation = Annotated[
    str, Field(description="A citation from search_docs, such as guides/llm-access-guide.md#retry-logic.")
]
EnvironmentName = Annotated[str | None, Field(description="Environment name; defaults to the current one.")]
DraftIds = Annotated[list[str], Field(min_length=1, description="IDs returned by the draft tools.")]
Summary = Annotated[str, Field(min_length=5, max_length=200, description="One-line summary of the change.")]
ChangeId = Annotated[str, Field(description="The change_id returned by prepare_change.")]
ApprovalHash = Annotated[str, Field(description="The approval_hash returned by prepare_change.")]
OptionalChangeId = Annotated[str | None, Field(description="A change ID; omit to list recent changes.")]


def search_docs(ctx: ToolContext, query: str, limit: int = 5) -> str:
    hits = ctx.runtime.docs().search(query, limit=limit)
    if not hits:
        return "No matching section in the upstream Citadel documentation."
    return "\n\n".join(
        f"### {hit.section.title}\nCitation: {hit.section.citation}\nLink: {hit.url}\n{hit.snippet}"
        for hit in hits
    )


def read_doc(ctx: ToolContext, citation: str) -> str:
    index = ctx.runtime.docs()
    text = index.read(citation)
    if text is None:
        raise ToolInputError(f"No section {citation}; use search_docs to find a citation")
    section = index.find(citation)
    link = index.url(section) if section else None
    return f"Source: {link or citation}\n\n{text}"


def gateway_overview(ctx: ToolContext, environment: str | None = None) -> str:
    env_name = ctx.environment_name(environment)
    env = ctx.environment(env_name)
    runtime = ctx.runtime
    report = assess_release(runtime.live_release(env), runtime.lock.upstream.release)
    lines = [
        f"## Gateway {env_name} ({env.environment_class}, {env.mode.value} mode)",
        f"* Gateway URL: {env.gateway_url}",
        f"* Agent upstream commit: {runtime.lock.upstream.commit[:12]}",
        f"* Compatibility: {report.overall.value}",
        f"* You can deploy directly: {runtime.can_deploy(env)}",
        "",
        "| Track | Live | Tested | Verdict |",
        "|---|---|---|---|",
    ]
    for track, assessment in sorted(report.assessments.items()):
        tested = ", ".join(assessment.tested) or "-"
        lines.append(f"| {track} | {assessment.live or '-'} | {tested} | {assessment.level.value} |")
    lines.extend(f"* {a.track}: {a.reason}" for a in report.problems())
    try:
        contract = GatewayClient(env.gateway_url).backend_contract()
    except Exception as exc:
        lines.append(f"\nThe live backend contract could not be read: {exc}")
    else:
        lines += [
            "",
            f"Models: {', '.join(sorted(contract.model_names())) or 'none'}",
            f"Backends: {len(contract.backends())}, pools: {len(contract.pools())}, "
            f"aliases: {len(contract.aliases())}",
            f"Circuit breaker: {'on' if contract.circuit_breaker_enabled else 'off'}, "
            f"session affinity: {'on' if contract.session_affinity_enabled else 'off'}",
        ]
    return "\n".join(lines)


def prepare_change(ctx: ToolContext, draft_ids: list[str], summary: str) -> str:
    try:
        drafts = [ctx.drafts.load(draft_id) for draft_id in draft_ids]
    except DraftError as exc:
        raise ToolInputError(str(exc)) from exc
    environments = {d.intent.environment for d in drafts}
    if len(environments) != 1:
        raise ToolInputError("All drafts in one change must target the same environment")
    request = ChangeRequest(
        summary=summary,
        environment=environments.pop(),
        changes=tuple(IntentChange(d.step_id, d.intent, d.operation, d.depends_on) for d in drafts),
        prerequisites=tuple(
            {
                p["stepId"]: PrerequisiteStep(
                    step_id=p["stepId"],
                    description=p["description"],
                    commands=tuple(p.get("commands") or ()),
                    verification=p.get("verification", ""),
                )
                for d in drafts
                for p in d.prerequisites
            }.values()
        ),
    )
    try:
        prepared = ctx.engine().prepare(request)
    except ChangeError as exc:
        raise ToolInputError(f"The change could not be prepared: {exc}") from exc
    ctx.prepared[prepared.change.id] = list(draft_ids)
    return (
        f"{prepared.summary()}\n\nchange_id: {prepared.change.id}\napproval_hash: {prepared.content_hash}\n"
        "Nothing has changed yet. Explain the summary to the user and ask for approval before calling "
        "execute_change with this change_id and approval_hash."
    )


def execute_change(ctx: ToolContext, change_id: str, approval_hash: str) -> str:
    try:
        result = ctx.engine().execute(change_id, approval_hash)
    except (ChangeError, ChangeStoreError) as exc:
        raise ToolInputError(f"The change was not executed: {exc}") from exc
    for draft_id in ctx.prepared.pop(change_id, []):
        ctx.drafts.delete(draft_id)
    lines = [f"Change {change_id} ({result['mode']} mode): recorded as {result.get('recorded', 'unknown')}."]
    if result["mode"] == "direct":
        lines.append("The change is deployed.")
    if "pullRequest" in result:
        lines.append(f"Pull request: {result['pullRequest']['url']}")
    if "next" in result:
        lines.append(f"Next: {result['next']}")
    return "\n".join(lines)


def change_status(ctx: ToolContext, change_id: str | None = None) -> str:
    store = ctx.runtime.store
    if change_id is None:
        ids = store.list_ids()[:10]
        if not ids:
            return "No changes have been prepared in this workspace."
        rows = ["| Change | Environment | Mode | State | Summary |", "|---|---|---|---|---|"]
        for cid in ids:
            change = store.load(cid)
            state = store.status(cid).get("state", "prepared")
            rows.append(f"| {cid} | {change.environment} | {change.mode} | {state} | {change.summary} |")
        return "\n".join(rows)
    try:
        change = store.load(change_id)
    except ChangeStoreError as exc:
        raise ToolInputError(str(exc)) from exc
    status = store.status(change_id)
    lines = [f"## {change.id}: {change.summary}", f"* State: {status.get('state', 'prepared')}"]
    for step in change.ordered_steps():
        step_status = status.get("steps", {}).get(step.id, {})
        kind = "manual prerequisite" if step.kind is StepKind.PREREQUISITE else "deployment"
        lines.append(f"* Step {step.id} ({kind}): {step_status.get('status', 'pending')}")
        if step_status.get("error"):
            lines.append(f"  * Error: {step_status['error']}")
    pull_request = status.get("pullRequest")
    if pull_request:
        lines.append(f"* Pull request: {pull_request['url']}")
        provider = ctx.runtime.git_provider()
        if provider is not None:
            info = provider.pull_request_status(pull_request["number"])
            lines.append(f"  * {'merged' if info.merged else info.state}, checks: {info.checks}")
    for key in ("next", "recordError"):
        if status.get(key):
            lines.append(f"* {status[key]}")
    return "\n".join(lines)


def register_core_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    def search_docs_tool(query: Query, limit: Limit = 5) -> str:
        return search_docs(ctx, query, limit)

    def read_doc_tool(citation: Citation) -> str:
        return read_doc(ctx, citation)

    def gateway_overview_tool(environment: EnvironmentName = None) -> str:
        return gateway_overview(ctx, environment)

    def prepare_change_tool(draft_ids: DraftIds, summary: Summary) -> str:
        return prepare_change(ctx, draft_ids, summary)

    def execute_change_tool(change_id: ChangeId, approval_hash: ApprovalHash) -> str:
        return execute_change(ctx, change_id, approval_hash)

    def change_status_tool(change_id: OptionalChangeId = None) -> str:
        return change_status(ctx, change_id)

    registry.add(
        ToolSpec(
            "search_docs",
            search_docs_tool,
            "Search the pinned upstream Citadel documentation. Returns sections with citations and links.",
        )
    )
    registry.add(
        ToolSpec("read_doc", read_doc_tool, "Read one upstream documentation section by its citation.")
    )
    registry.add(
        ToolSpec(
            "gateway_overview",
            gateway_overview_tool,
            "Gateway versions, compatibility with this agent, change mode, models, backends and aliases.",
        )
    )
    registry.add(
        ToolSpec(
            "prepare_change",
            prepare_change_tool,
            "Validate drafts, render the contract files and preview the Azure changes. Changes nothing.",
        )
    )
    registry.add(
        ToolSpec(
            "execute_change",
            execute_change_tool,
            "Deploy (direct mode) or propose as a pull request (PR mode) a prepared change. "
            "Needs user approval.",
            requires_approval=True,
        )
    )
    registry.add(
        ToolSpec(
            "change_status", change_status_tool, "Status of a change: steps, deployments and pull request."
        )
    )
