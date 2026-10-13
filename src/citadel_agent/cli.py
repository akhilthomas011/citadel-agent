# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Command-line interface for the Citadel Agent."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from citadel_agent import __version__
from citadel_agent.cli_admin import bootstrap, doctor, env_app, llm_check, repo_app
from citadel_agent.cli_chat import chat, devui, usage
from citadel_agent.cli_contracts import contracts_app
from citadel_agent.paths import ProjectRootNotFoundError, find_project_root
from citadel_agent.proc import CommandError
from citadel_agent.upstream import build_lock, check_lock, read_lock, write_lock
from citadel_agent.upstream.sync import (
    SyncError,
    Verdict,
    apply_sync,
    render_markdown,
    run_sync,
    summary_rows,
)

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
app.command("usage")(usage)


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


_VERDICT_STYLE = {Verdict.SAFE: "green", Verdict.REVIEW: "yellow", Verdict.BLOCKED: "red"}


@upstream_app.command("sync")
def upstream_sync(
    ref: Annotated[
        str | None,
        typer.Option(help="Upstream branch or commit to move to (default: tip of the locked branch)."),
    ] = None,
    report: Annotated[Path | None, typer.Option(help="Write the Markdown compatibility report here.")] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Move the submodule and rewrite citadel.lock.json if compatible.")
    ] = False,
    check: Annotated[
        bool, typer.Option("--check", help="Exit 1 when the new commit is BLOCKED (for CI).")
    ] = False,
) -> None:
    """Compare the pinned upstream commit with a newer one and optionally move the pin.

    Dry run by default: nothing in the checkout changes. Exit codes: 0 ok, 1 blocked (with --check
    or --apply), 2 configuration error.
    """
    try:
        root = find_project_root()
        result = run_sync(root, ref=ref)
    except (ProjectRootNotFoundError, SyncError) as exc:
        raise _fail(str(exc)) from exc

    table = Table(title="Upstream sync", show_header=False)
    table.add_column("Property", style="bold")
    table.add_column("Value", overflow="fold")
    for name, value in summary_rows(result):
        style = _VERDICT_STYLE[result.verdict] if name == "Verdict" else None
        table.add_row(name, f"[{style}]{value}[/{style}]" if style else value)
    console.print(table)

    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_markdown(result), encoding="utf-8", newline="\n")
        console.print(f"Wrote {report}")

    blocked = result.verdict is Verdict.BLOCKED
    if apply and blocked:
        err_console.print(
            "[red]Refusing to apply:[/red] the new upstream commit is BLOCKED (see the report)."
        )
        raise typer.Exit(1)
    if apply:
        try:
            apply_sync(root, result)
        except SyncError as exc:
            raise _fail(str(exc)) from exc
        if result.unchanged:
            console.print("Already up to date; nothing changed.")
        else:
            console.print(
                f"[green]Moved the submodule to {result.new_commit[:7]} and updated the lock.[/green]"
                " Run the tests, then commit both."
            )
    else:
        console.print("Dry run: nothing was changed. Use --apply to move the pin.")
    if check and blocked:
        raise typer.Exit(1)


def main() -> None:
    """Console-script entry point."""
    try:
        app()
    except KeyboardInterrupt:
        err_console.print("Interrupted by user.")
        sys.exit(130)
