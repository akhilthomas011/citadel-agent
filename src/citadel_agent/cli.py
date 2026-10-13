# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Command-line interface for the Citadel Agent."""

from __future__ import annotations

import sys

import typer
from rich.console import Console
from rich.table import Table

from citadel_agent import __version__
from citadel_agent.cli_admin import bootstrap, doctor, env_app, llm_check, repo_app
from citadel_agent.cli_chat import chat, devui
from citadel_agent.cli_contracts import contracts_app
from citadel_agent.paths import ProjectRootNotFoundError, find_project_root
from citadel_agent.proc import CommandError
from citadel_agent.upstream import build_lock, check_lock, read_lock, write_lock

EXIT_CONFIG_ERROR = 2

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="citadel-agent",
    help="AI assistant for using and operating the Citadel Governance Hub (Azure API Management AI gateway).",
    no_args_is_help=True,
    add_completion=False,
)
upstream_app = typer.Typer(
    help="Inspect and manage the pinned upstream Citadel repository.", no_args_is_help=True
)
app.add_typer(upstream_app, name="upstream")
app.add_typer(contracts_app, name="contracts")
app.add_typer(env_app, name="env")
app.add_typer(repo_app, name="repo")
app.command("bootstrap")(bootstrap)
app.command("doctor")(doctor)
app.command("llm-check")(llm_check)
app.command("chat")(chat)
app.command("devui")(devui)


def _fail(message: str, code: int = EXIT_CONFIG_ERROR) -> typer.Exit:
    err_console.print(f"[red]Error:[/red] {message}")
    return typer.Exit(code)


@app.command()
def version() -> None:
    """Show the agent version and the pinned upstream Citadel release."""
    try:
        root = find_project_root()
        lock = read_lock(root)
    except (ProjectRootNotFoundError, FileNotFoundError) as exc:
        raise _fail(str(exc)) from exc

    table = Table(title="Citadel Agent", show_header=False)
    table.add_column("Property", style="bold")
    table.add_column("Value", overflow="fold")
    table.add_row("Agent version", __version__)
    table.add_row("Upstream repository", lock.upstream.repository)
    table.add_row("Upstream branch", lock.upstream.branch)
    table.add_row("Upstream commit", lock.upstream.commit)
    for track, value in lock.upstream.release.items():
        table.add_row(track, value)
    console.print(table)


@upstream_app.command("status")
def upstream_status() -> None:
    """Check that the upstream submodule matches citadel.lock.json."""
    try:
        status = check_lock(find_project_root())
    except (ProjectRootNotFoundError, FileNotFoundError, CommandError) as exc:
        raise _fail(str(exc)) from exc

    if status.in_sync:
        console.print(f"[green]In sync[/green] with upstream commit {status.locked_commit}.")
        return
    if not status.commit_matches:
        console.print(
            f"[yellow]Commit drift:[/yellow] lock={status.locked_commit} "
            f"checked out={status.checked_out_commit}"
        )
    if not status.release_matches:
        console.print("[yellow]release.json drift[/yellow] between the lock and the checked-out upstream.")
    raise typer.Exit(1)


@upstream_app.command("lock")
def upstream_lock() -> None:
    """Record the checked-out upstream submodule commit and release tracks in citadel.lock.json."""
    try:
        root = find_project_root()
        path = write_lock(root, build_lock(root))
    except (ProjectRootNotFoundError, FileNotFoundError, CommandError) as exc:
        raise _fail(str(exc)) from exc
    console.print(f"Wrote {path}")


def main() -> None:
    """Console-script entry point."""
    try:
        app()
    except KeyboardInterrupt:
        err_console.print("Interrupted by user.")
        sys.exit(130)
