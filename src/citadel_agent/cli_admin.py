# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Administrative commands: environment discovery, repository setup, bootstrap and diagnostics."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.table import Table

from citadel_agent.azure_clients.arm import ArmClient
from citadel_agent.azure_clients.credentials import create_credential, describe_principal
from citadel_agent.bootstrap import BootstrapError, run_bootstrap
from citadel_agent.changes.engine import ChangeError
from citadel_agent.changes.repo_init import RepoInitError, init_canonical_repository
from citadel_agent.discovery import DiscoveryError, discover_environment
from citadel_agent.paths import find_project_root
from citadel_agent.runtime import build_runtime
from citadel_agent.settings import Settings
from citadel_agent.upstream.compat import assess_release
from citadel_agent.upstream.lock import read_lock

console = Console()
err_console = Console(stderr=True)

env_app = typer.Typer(help="Discover Citadel environments.", no_args_is_help=True)
repo_app = typer.Typer(help="Manage the canonical contracts repository.", no_args_is_help=True)


@env_app.command("discover")
def env_discover(
    subscription: Annotated[str, typer.Option(help="Subscription ID of the hub.")],
    resource_group: Annotated[str, typer.Option(help="Resource group of the hub.")],
    name: Annotated[str, typer.Option(help="Environment key for the admin policy.")] = "dev",
    environment_class: Annotated[str, typer.Option("--class", help="dev, test or prod.")] = "dev",
    mode: Annotated[str, typer.Option(help="direct or pr (prod is always pr).")] = "direct",
) -> None:
    """Print an admin-policy environment block for a deployed hub."""
    credential = create_credential()
    try:
        block = discover_environment(
            ArmClient(credential),
            subscription,
            resource_group,
            environment_class=environment_class,
            mode=mode,
            tenant_id=describe_principal(credential).tenant_id,
        )
    except DiscoveryError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(yaml.safe_dump({"environments": {name: block}}, sort_keys=False), end="")


@repo_app.command("init")
def repo_init(
    path: Annotated[Path, typer.Option(help="Folder for the new canonical repository.")],
    policy: Annotated[Path, typer.Option(help="Admin policy YAML to commit.")],
) -> None:
    """Create a canonical contracts repository pinned to the agent's tested upstream commit."""
    root = find_project_root()
    lock = read_lock(root)
    try:
        repo = init_canonical_repository(
            path,
            template_dir=root / "templates" / "contracts-repo",
            lock=lock,
            upstream_source=str(root / lock.upstream.path),
            admin_policy_text=policy.read_text(encoding="utf-8"),
        )
    except (RepoInitError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Created {repo.root}[/green] (upstream {lock.upstream.commit[:12]})")
    console.print(
        "Next: push it to your git host and set CITADEL_AGENT_REPO_URL to its URL, or set "
        f"CITADEL_AGENT_REPO_URL={repo.root.as_posix()} for local-only development."
    )


def bootstrap(
    environment: Annotated[
        str | None, typer.Option(help="Environment (defaults to agent.environment).")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Approve without prompting.")] = False,
) -> None:
    """Create the agent's own access contract (its LLM access through the gateway)."""
    runtime = build_runtime(Settings())

    def confirm(summary: str) -> bool:
        console.print(summary)
        return yes or typer.confirm("Apply this change?", default=False)

    try:
        result = run_bootstrap(runtime, confirm=confirm, environment=environment)
    except (BootstrapError, ChangeError, PermissionError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(result)


def doctor() -> None:
    """Show who you are, which policy applies and whether the gateway is compatible."""
    runtime = build_runtime(Settings())
    table = Table(title="Citadel Agent diagnostics", show_header=False)
    table.add_column("Item", style="bold")
    table.add_column("Value", overflow="fold")
    for key, value in runtime.describe().items():
        table.add_row(key, str(value))
    if runtime.policy is not None:
        env = runtime.environment()
        live = runtime.live_release(env)
        report = assess_release(live, runtime.lock.upstream.release)
        table.add_row(
            "environment", f"{runtime.environment_name()} ({env.environment_class}, mode {env.mode.value})"
        )
        table.add_row("gateway", env.gateway_url)
        table.add_row("compatibility", report.overall.value)
        for problem in report.problems():
            table.add_row(f"  {problem.track}", f"{problem.level.value}: {problem.reason}")
        table.add_row("can deploy directly", str(runtime.can_deploy(env)))
    console.print(table)


def llm_check(
    prompt: Annotated[str, typer.Option(help="Question for the model.")] = "What is 17 * 23?",
) -> None:
    """Verify the agent's LLM access through the gateway: a tool call plus a streamed answer."""
    from agent_framework import Agent

    from citadel_agent.llm_client import LlmConfigurationError, create_chat_client

    runtime = build_runtime(Settings())
    calls: list[str] = []

    def multiply(a: int, b: int) -> int:
        """Multiply two integers."""
        calls.append(f"multiply({a}, {b})")
        return a * b

    try:
        client = create_chat_client(runtime)
    except LlmConfigurationError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    agent = Agent(
        client=client,
        name="CitadelLlmCheck",
        instructions="Use the multiply tool for arithmetic. Answer in one short sentence.",
        tools=[multiply],
    )

    async def run() -> str:
        chunks: list[str] = []
        async for update in agent.run(prompt, stream=True):
            if update.text:
                chunks.append(update.text)
        return "".join(chunks)

    try:
        answer = asyncio.run(run())
    except Exception as exc:
        from citadel_agent.safety.redaction import default_redactor

        err_console.print(f"[red]The gateway call failed: {default_redactor().redact(str(exc))[:800]}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"tool calls: {calls or 'none'}")
    console.print(f"streamed answer: {answer}")
    if not calls:
        err_console.print("[yellow]The model did not call the tool.[/yellow]")
        raise typer.Exit(1)
