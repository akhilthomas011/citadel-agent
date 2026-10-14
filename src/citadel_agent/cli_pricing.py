# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""``citadel-agent pricing``: review and seed the ``model-pricing`` container used by cost reports."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from citadel_agent.azure_clients.cosmos import MODEL_PRICING_CONTAINER, CosmosUsageReader, cosmos_endpoint
from citadel_agent.gateway.client import GatewayClient
from citadel_agent.pricing import (
    PricingError,
    load_entries,
    plan_changes,
    sample_file,
    select_entries,
    validate_entry,
)
from citadel_agent.runtime import build_runtime
from citadel_agent.settings import Settings

console = Console()
err_console = Console(stderr=True)

pricing_app = typer.Typer(
    help="Review and seed model prices for cost reports (operators).", no_args_is_help=True
)


def _fail(message: str, code: int = 1) -> typer.Exit:
    err_console.print(f"[red]{message}[/red]")
    return typer.Exit(code)


@pricing_app.command("seed")
def seed(
    file: Annotated[
        Path | None, typer.Option(help="Reviewed pricing JSON (default: upstream's sample).")
    ] = None,
    all_models: Annotated[
        bool, typer.Option("--all", help="Also seed models the gateway does not serve.")
    ] = False,
    apply: Annotated[bool, typer.Option("--apply", help="Write the prices. Default is a preview.")] = False,
    environment: Annotated[str | None, typer.Option(help="Environment to work with.")] = None,
) -> None:
    """Preview, or with --apply write, prices for the models the gateway serves.

    The sample file lists public prices at the time upstream wrote it. Review every price before
    you apply: they decide the cost numbers in reports.
    """
    settings = Settings()
    if environment:
        settings = settings.model_copy(update={"environment": environment})
    runtime = build_runtime(settings)
    if runtime.caller.persona.value != "operator":
        raise _fail("Seeding prices is an operator task: ask your platform team.", 1)
    env = runtime.environment()
    if env.cosmos is None:
        raise _fail("No Cosmos DB account is configured for this environment.", 2)

    path = file or sample_file(runtime.project_root / Path(*runtime.lock.upstream.path.split("/")))
    try:
        entries = load_entries(path)
    except PricingError as exc:
        raise _fail(str(exc), 2) from exc
    problems = [p for entry in entries for p in validate_entry(entry)]
    if problems:
        raise _fail("The pricing file has problems:\n  " + "\n  ".join(problems), 2)

    contract = GatewayClient(env.gateway_url).backend_contract()
    # An alias is a client-facing name. Usage is recorded under the real model, so it needs no price.
    aliases = {str(a.get("name", "")).lower() for a in contract.aliases()}
    deployed = sorted(m for m in contract.model_names() if m.lower() not in aliases)
    kept, skipped = select_entries(entries, None if all_models else deployed)

    reader = CosmosUsageReader(runtime.credential, cosmos_endpoint(env.cosmos.account_name))
    database = env.cosmos.database or reader.discover_usage_database()
    if database is None:
        raise _fail("The usage database was not found in Cosmos DB.", 2)
    existing = reader.query(database, MODEL_PRICING_CONTAINER, "SELECT * FROM c")
    plan = plan_changes(kept, existing, skipped=skipped, deployed_models=deployed)

    console.print(f"Pricing file: {path}")
    console.print(f"Gateway models: {', '.join(deployed) or 'none'}")
    console.print(plan.render())
    if not apply:
        console.print("\nPreview only. Review the prices, then run again with --apply.")
        return
    for item in plan.writes:
        reader.upsert(database, MODEL_PRICING_CONTAINER, item)
    console.print(f"\n[green]Wrote {len(plan.writes)} price entries to {MODEL_PRICING_CONTAINER}.[/green]")
