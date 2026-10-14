# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Interactive chat and DevUI entry points."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
import warnings
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text

from citadel_agent.changes.approval import approval_details, git_base_reader
from citadel_agent.changes.engine import PreparedChange
from citadel_agent.changes.store import ChangeStoreError
from citadel_agent.settings import Settings
from citadel_agent.tools.context import ToolContext
from citadel_agent.usage_log import USAGE_FILE, TurnUsage, UsageLog

console = Console()
EXIT_COMMANDS = {"/exit", "/quit", "exit", "quit"}


class _ConsumedApprovalFilter(logging.Filter):
    """Hide Agent Framework's notice about approval responses it already consumed.

    When the framework rescans the conversation it drops approval responses that were already
    bound to their request and logs a warning; the approval itself was applied once.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "did not match the active approval occurrence" not in record.getMessage()


def use_utf8_console() -> None:
    """Model output contains characters a Windows code page cannot encode; switch stdout to UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    logging.getLogger("agent_framework").addFilter(_ConsumedApprovalFilter())


def configure_telemetry(settings: Settings) -> None:
    """Opt-in OpenTelemetry (prompts and responses are never exported)."""
    if not (settings.otel_endpoint or settings.otel_console):
        return
    from agent_framework.observability import configure_otel_providers

    configure_otel_providers(
        service_name="citadel-agent",
        otlp_endpoint=settings.otel_endpoint,
        enable_console_exporters=settings.otel_console,
        enable_sensitive_data=False,
    )


def parse_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            value = json.loads(arguments)
        except ValueError:
            return {"arguments": arguments}
        return value if isinstance(value, dict) else {"arguments": value}
    return {}


def _contract_details(context: ToolContext, change: Any) -> str:
    """The contract values that change, read from the stored snapshot and the base commit."""
    try:
        snapshot = context.runtime.store.snapshot(change.id)
    except ChangeStoreError:
        snapshot = {}
    root = getattr(getattr(context.runtime, "workspace", None), "path", None)
    read_base = git_base_reader(Path(root), change.repo_base_commit) if root else (lambda _path: None)
    return approval_details(change.files, snapshot, read_base)


def describe_approval(context: ToolContext, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Text for an approval prompt and whether the request must be refused outright.

    For ``execute_change`` the text comes from the stored, immutable change set, never from the
    model's description, and a request whose approval hash does not match is refused.
    """
    if name != "execute_change":
        return f"{name}({json.dumps(arguments)})", False
    change_id = str(arguments.get("change_id", ""))
    try:
        change = context.runtime.store.load(change_id)
    except ChangeStoreError as exc:
        return f"Unknown change {change_id!r}: {exc}", True
    expected = change.content_hash()
    summary = PreparedChange(change, expected).summary()
    details = _contract_details(context, change)
    if details:
        summary = f"{summary}\n\n{details}"
    if arguments.get("approval_hash") != expected:
        return f"{summary}\n\nThe approval hash in the request does not match this change set.", True
    action = (
        "deploy this change now"
        if change.mode == "direct"
        else "open a pull request (the pipeline deploys after review)"
    )
    return f"{summary}\n\nApproving will {action}.", False


def typed_confirmation(context: ToolContext, arguments: dict[str, Any]) -> str | None:
    """A word the user must type for irreversible steps (regenerating a key), else None."""
    try:
        snapshot = context.runtime.store.snapshot(str(arguments.get("change_id", "")))
    except ChangeStoreError:
        return None
    for path, content in snapshot.items():
        if path.startswith(".citadel/rotations/") and '"phase": "rotate"' in content:
            return "ROTATE"
    return None


async def run_turn(agent: Any, session: Any, message: Any, approve: Any) -> TurnUsage:
    """Send one user message, stream the answer and resolve approval requests.

    Returns the model usage of the turn, one entry per model request.
    """
    from agent_framework import Message

    usage = TurnUsage()
    pending = message
    while True:
        stream = agent.run(pending, session=session, stream=True)
        seen = len(usage.requests)
        async for update in stream:
            usage.add_update(update)
            if update.text:
                sys.stdout.write(update.text)
                sys.stdout.flush()
        final = await stream.get_final_response()
        if len(usage.requests) == seen:
            usage.add_details(getattr(final, "usage_details", None))
        sys.stdout.write("\n")
        requests = list(final.user_input_requests or [])
        if not requests:
            return usage
        responses = [request.to_function_approval_response(approve(request)) for request in requests]
        pending = Message(role="user", contents=responses)


def chat(
    environment: Annotated[str | None, typer.Option(help="Environment to work with.")] = None,
    resume: Annotated[str | None, typer.Option(help="Session ID to resume.")] = None,
) -> None:
    """Chat with Citadel Agent (type /exit to leave, /new for a new conversation)."""
    from agent_framework import FileSessionStore

    from citadel_agent.agent import create_agent
    from citadel_agent.llm_client import LlmConfigurationError
    from citadel_agent.runtime import build_runtime

    use_utf8_console()
    warnings.filterwarnings("ignore", message=r"\[SESSION_STORE\]")
    settings = Settings()
    if environment:
        settings = settings.model_copy(update={"environment": environment})
    configure_telemetry(settings)
    runtime = build_runtime(settings)
    try:
        bundle = create_agent(runtime)
    except LlmConfigurationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    sessions = FileSessionStore(runtime.store.directory.parent / "sessions")
    usage_log = UsageLog(runtime.store.directory.parent / USAGE_FILE)

    def approve(request: Any) -> bool:
        call = getattr(request, "function_call", None)
        name = getattr(call, "name", "tool")
        arguments = parse_arguments(getattr(call, "arguments", None))
        text, refuse = describe_approval(bundle.context, name, arguments)
        console.print(
            Panel(Text(text), title=f"Approval required: {name}", border_style="red" if refuse else "yellow")
        )
        if refuse:
            console.print("[red]Refused automatically.[/red]")
            return False
        word = typed_confirmation(bundle.context, arguments) if name == "execute_change" else None
        if word:
            console.print(
                "[red]This step permanently replaces the old key. Clients still using it will fail.[/red]"
            )
            return Prompt.ask(f"Type {word} to confirm").strip() == word
        return Confirm.ask("Approve?", default=False)

    async def main() -> None:
        session = await sessions.get(resume) if resume else None
        session = session or bundle.agent.create_session()
        policy = runtime.policy
        env_name = settings.environment or (policy.default_environment if policy else "-")
        console.print(
            Panel(
                f"Signed in as {runtime.principal.label} ({runtime.caller.persona.value}). "
                f"Environment: {env_name}.\nSession {session.session_id}. Type /exit to leave.",
                title="Citadel Agent",
            )
        )
        while True:
            text = Prompt.ask("[bold cyan]you[/bold cyan]").strip()
            if not text:
                continue
            if text.lower() in EXIT_COMMANDS:
                break
            if text.lower() == "/new":
                session = bundle.agent.create_session()
                console.print(f"New session {session.session_id}.")
                continue
            console.print("[bold green]agent[/bold green]")
            try:
                usage = await run_turn(bundle.agent, session, text, approve)
                route = bundle.router.last if bundle.router is not None else None
                tool_names = [t.name for t in bundle.router.core] if bundle.router is not None else []
                usage_log.record(
                    usage,
                    session_id=session.session_id,
                    tools=[*tool_names, *(route.tool_names if route else ())],
                    domains=route.domains if route else (),
                )
                if usage.requests:
                    console.print(f"[dim]{usage.summary()}[/dim]")
            except Exception as exc:
                from citadel_agent.safety.redaction import default_redactor

                console.print(f"[red]{default_redactor().redact(str(exc))[:500]}[/red]")
            await sessions.set(session.session_id, session)
        console.print(f"Resume this conversation with: citadel-agent chat --resume {session.session_id}")

    asyncio.run(main())


def usage(
    last: Annotated[int, typer.Option(help="Show only the most recent N requests (0 for all).")] = 0,
) -> None:
    """Summarize model usage per request from the local usage log (counts only, never content)."""
    from rich.table import Table

    from citadel_agent.paths import find_project_root, state_dir
    from citadel_agent.usage_log import USAGE_FILE, summarize

    settings = Settings()
    path = (settings.state_dir or state_dir(find_project_root())) / USAGE_FILE
    totals = summarize(path, last=last)
    if not totals:
        console.print(f"No usage recorded yet. Chat with the agent first ({path}).")
        return
    table = Table(title="Model usage per request", show_header=False)
    table.add_column("Measure", style="bold")
    table.add_column("Value", justify="right")
    labels = {
        "requests": "Model requests",
        "input_total": "Input tokens (total)",
        "output_total": "Output tokens (total)",
        "input_average": "Input per request (average)",
        "input_p90": "Input per request (90th percentile)",
        "input_max": "Input per request (largest)",
    }
    for key, label in labels.items():
        table.add_row(label, f"{totals[key]:,}")
    console.print(table)


def devui(
    port: Annotated[int, typer.Option(help="Port for the DevUI web server.")] = 8080,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open a browser.")] = True,
) -> None:
    """Serve the agent in the Agent Framework DevUI (local development only)."""
    try:
        from agent_framework.devui import serve
    except ImportError as exc:
        console.print("[red]DevUI is not installed. Run: uv sync --all-extras[/red]")
        raise typer.Exit(1) from exc

    from citadel_agent.agent import create_agent
    from citadel_agent.approval_ui import devui_middleware
    from citadel_agent.runtime import build_runtime

    use_utf8_console()
    warnings.filterwarnings("ignore", message=r"\[SESSION_STORE\]")
    settings = Settings()
    configure_telemetry(settings)
    bundle = create_agent(build_runtime(settings), middleware_factory=devui_middleware)
    # The agent can deploy changes, so the local API always requires a token.
    token = secrets.token_urlsafe(32)
    console.print(
        Panel(
            f"DevUI: http://127.0.0.1:{port}\nAccess token (enter it when the DevUI asks): {token}",
            title="Citadel Agent DevUI",
        )
    )
    serve(entities=[bundle.agent], port=port, auto_open=open_browser, auth_enabled=True, auth_token=token)
