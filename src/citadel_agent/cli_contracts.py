# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""``citadel-agent contracts`` commands used by people and by the contracts repository's CI/CD.

These commands never involve a language model: they validate, render, preview and deploy the
contracts exactly as the agent does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from citadel_agent.azure_clients.bicep import compile_deployment
from citadel_agent.azure_clients.credentials import create_credential
from citadel_agent.azure_clients.deployments import NO_CHANGE_TYPES, DeploymentRunner, DeploymentTarget
from citadel_agent.changes.changeset import ChangeSet, StepKind
from citadel_agent.changes.repository import CanonicalRepository
from citadel_agent.changes.validation import IssueLevel, validate_repository
from citadel_agent.contracts.intent_io import load_intent
from citadel_agent.contracts.render import RenderContext, render_intent
from citadel_agent.knowledge.catalogs import load_catalogs
from citadel_agent.policy.admin import load_admin_policy

console = Console()
err_console = Console(stderr=True)

contracts_app = typer.Typer(help="Validate, render, preview and deploy contracts in a canonical repository.")

RepoOption = Annotated[Path, typer.Option("--repo", help="Path to the canonical contracts repository.")]
ManifestOption = Annotated[
    Path, typer.Option("--manifest", help="Change-set manifest (.citadel/changesets/<id>.json).")
]


def _load_manifest(repo: Path, manifest: Path) -> ChangeSet:
    path = manifest if manifest.is_absolute() else repo / manifest
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("contentHash", None)
    return ChangeSet.model_validate(data)


@contracts_app.command("validate")
def validate(
    repo: RepoOption = Path("."),
    base: Annotated[str | None, typer.Option(help="Git ref holding the trusted admin policy.")] = None,
    compile_contracts: Annotated[
        bool, typer.Option("--compile", help="Also compile with the Bicep CLI.")
    ] = False,
) -> None:
    """Validate every contract (fails with exit code 1 on errors)."""
    repository = CanonicalRepository(repo)
    loaded = load_admin_policy(repository.root, ref=base)
    issues = validate_repository(repository, loaded, compile_contracts=compile_contracts)
    for issue in issues:
        style = "red" if issue.level is IssueLevel.ERROR else "yellow"
        console.print(f"[{style}]{issue}[/{style}]")
    errors = [i for i in issues if i.level is IssueLevel.ERROR]
    if errors:
        err_console.print(f"{len(errors)} error(s) found.")
        raise typer.Exit(1)
    console.print("[green]All contracts are valid.[/green]")


@contracts_app.command("render")
def render(
    repo: RepoOption = Path("."),
    base: Annotated[str | None, typer.Option(help="Git ref holding the trusted admin policy.")] = None,
) -> None:
    """Regenerate every generated file from the contract intents (after editing contract.yaml by hand)."""
    repository = CanonicalRepository(repo)
    loaded = load_admin_policy(repository.root, ref=base)
    if loaded.policy is None:
        err_console.print(f"[red]Admin policy unavailable:[/red] {loaded.error}")
        raise typer.Exit(2)
    commit = repository.upstream_commit()
    for relative in repository.iter_intent_files():
        intent = load_intent(repository.path(relative))
        env = loaded.policy.environment(intent.environment)
        rendered = render_intent(
            intent,
            RenderContext(
                repo_root=repository.root,
                upstream_root=repository.upstream_root,
                target=env.to_target(intent.environment),
                upstream_commit=commit,
            ),
        )
        rendered.write(repository.root)
        console.print(f"rendered {rendered.directory}")


def _runner(subscription_id: str) -> DeploymentRunner:
    return DeploymentRunner(create_credential(), subscription_id)


@contracts_app.command("whatif")
def whatif(repo: RepoOption = Path("."), manifest: ManifestOption = Path()) -> None:
    """Preview a change set's deploy steps (prints a Markdown summary)."""
    change = _load_manifest(repo, manifest)
    console.print(f"### What-if for change set `{change.id}` ({change.environment})\n")
    failed: list[str] = []
    for step in change.ordered_steps():
        if step.kind is not StepKind.DEPLOY:
            console.print(f"* `{step.id}`: manual prerequisite: {step.description}")
            continue
        compiled = compile_deployment(repo / Path(*(step.parameters_file or "").split("/")))
        target = DeploymentTarget(
            scope="subscription", subscription_id=step.subscription_id or "", location=step.location
        )
        report = _runner(step.subscription_id or "").what_if(target, step.deployment_name or "", compiled)
        counts = ", ".join(f"{k} {v}" for k, v in sorted(report.counts().items())) or "no changes"
        if report.error:
            counts = "what-if failed"
            failed.append(step.id)
        console.print(f"* `{step.id}` ({step.parameters_file}): {counts}")
        catalogs = load_catalogs()
        created = [c.resource_id for c in report.changes if c.change_type == "Create" and not c.potential]
        for change_item in report.changes:
            if change_item.change_type not in NO_CHANGE_TYPES:
                noise = catalogs.classify_noise(
                    change_item.resource_id,
                    change_item.change_type,
                    change_item.property_changes,
                    created=created,
                )
                suffix = f" (expected, no real change: {noise.explanation})" if noise else ""
                console.print(f"  * {change_item.label}: `{change_item.resource_id}`{suffix}")
        for diagnostic in report.diagnostics:
            console.print(f"  * note: {diagnostic}")
        if report.error:
            console.print(f"  * error: {report.error}")
    if failed:
        err_console.print(f"[red]What-if failed for: {', '.join(failed)}[/red]")
        raise typer.Exit(1)


@contracts_app.command("deploy")
def deploy(repo: RepoOption = Path("."), manifest: ManifestOption = Path()) -> None:
    """Deploy a merged change set step by step (used by the citadel-deploy workflow)."""
    change = _load_manifest(repo, manifest)
    if change.executed_by == "direct":
        console.print(f"Change set {change.id} was deployed directly; nothing to do.")
        return
    for step in change.ordered_steps():
        if step.kind is not StepKind.DEPLOY:
            console.print(f"Skipping manual prerequisite '{step.id}': {step.description}")
            continue
        compiled = compile_deployment(repo / Path(*(step.parameters_file or "").split("/")))
        if compiled.parameters_hash != step.parameters_hash or compiled.template_hash != step.template_hash:
            err_console.print(f"[red]{step.id}: compiled output differs from the prepared change set.[/red]")
            raise typer.Exit(1)
        target = DeploymentTarget(
            scope="subscription", subscription_id=step.subscription_id or "", location=step.location
        )
        runner = _runner(step.subscription_id or "")
        report = runner.what_if(target, step.deployment_name or "", compiled)
        if report.error:
            err_console.print(f"[red]{step.id}: what-if failed, not deploying: {report.error}[/red]")
            raise typer.Exit(1)
        console.print(f"{step.id}: what-if {report.counts()}")
        record = runner.deploy(target, step.deployment_name or "", compiled)
        if not record.succeeded:
            err_console.print(
                f"[red]{step.id}: deployment {record.name} {record.provisioning_state}: {record.error}[/red]"
            )
            raise typer.Exit(1)
        console.print(f"[green]{step.id}: deployed as {record.name}[/green]")
