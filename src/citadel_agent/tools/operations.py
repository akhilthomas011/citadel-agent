# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Operations tools: usage and cost, the live backend contract, and preflight checks.

(No postponed annotations; see ``tools.core``.)
"""

import re
import time
from collections import defaultdict
from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from citadel_agent.azure_clients.cosmos import (
    LLM_USAGE_CONTAINER,
    MODEL_PRICING_CONTAINER,
    CosmosUsageReader,
    cosmos_endpoint,
)
from citadel_agent.azure_clients.keyvault import KeyVaultAccessError, KeyVaultReader, vault_url
from citadel_agent.connectivity import diagnose
from citadel_agent.gateway.client import GatewayClient
from citadel_agent.knowledge.catalogs import load_catalogs
from citadel_agent.tools.context import ToolContext
from citadel_agent.tools.registry import ToolInputError, ToolRegistry, ToolSpec

GroupBy = Literal["model", "product", "app", "day"]
_DISPLAY_NAME = re.compile(r"^[A-Za-z0-9 _.-]{1,120}$")
TOKEN_METRICS = {
    "Prompt Tokens": "prompt",
    "Completion Tokens": "completion",
    "Prompt Cached Tokens": "cached",
}
EnvironmentName = Annotated[str | None, Field(description="Environment name; defaults to the current one.")]


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _scope_products(
    ctx: ToolContext, env_name: str, business_unit: str | None, use_case: str | None
) -> list[str] | None:
    """Product display names in scope (usage data is keyed by display name), None for all."""
    if business_unit and use_case:
        intent = ctx.owned_access_contract(business_unit, use_case, env_name)
        return [intent.product_display_name(s.code) for s in intent.services]
    if ctx.persona == "operator":
        return None
    authorizer = ctx.runtime.authorizer
    return [
        record.intent.product_display_name(service.code)
        for record in ctx.access_contracts(env_name)
        if authorizer.owns(record.intent)
        for service in record.intent.services
    ]


def _cosmos_usage(
    ctx: ToolContext, env: Any, products: list[str] | None, days: int
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if env.cosmos is None:
        raise ToolInputError("No Cosmos DB account is configured for this environment")
    reader = CosmosUsageReader(ctx.runtime.credential, cosmos_endpoint(env.cosmos.account_name))
    database = env.cosmos.database or reader.discover_usage_database()
    if database is None:
        raise ToolInputError("The usage database was not found in Cosmos DB")
    sql = (
        "SELECT c.productName, c.model, c.deploymentName, c.appId, c.promptTokens, c.responseTokens, "
        "c.promptCachedTokens, c.timestamp FROM c WHERE c._ts >= @since"
    )
    parameters: dict[str, Any] = {"since": int(time.time()) - days * 86400}
    if products is not None:
        sql += " AND ARRAY_CONTAINS(@products, c.productName)"
        parameters["products"] = products
    rows = [
        {
            "product": row.get("productName") or "-",
            "model": row.get("model") or row.get("deploymentName") or "-",
            "app": row.get("appId") or "-",
            "day": str(row.get("timestamp") or "")[:10] or "-",
            "prompt": _int(row.get("promptTokens")),
            "completion": _int(row.get("responseTokens")),
            "cached": _int(row.get("promptCachedTokens")),
        }
        for row in reader.query(database, LLM_USAGE_CONTAINER, sql, parameters)
    ]
    pricing: dict[str, dict[str, Any]] = {}
    try:
        for entry in reader.query(
            database, MODEL_PRICING_CONTAINER, "SELECT * FROM c WHERE c.isActive = true"
        ):
            for key in (entry.get("model"), entry.get("deploymentName")):
                if key:
                    pricing[str(key).lower()] = entry
    except Exception:
        pricing = {}
    return rows, pricing


def _metric_usage(ctx: ToolContext, env: Any, products: list[str] | None, days: int) -> list[dict[str, Any]]:
    workspace = env.log_analytics_workspace_id
    if not workspace:
        raise ToolInputError("No Log Analytics workspace is configured for this environment")
    names = ", ".join(f"'{name}'" for name in TOKEN_METRICS)
    kql = (
        f"AppMetrics\n| where Name in ({names})\n"
        "| extend product = tostring(Properties.productName), model = tostring(Properties.deploymentName), "
        "app = tostring(Properties.appId)\n"
    )
    if products is not None:
        safe = [p for p in products if _DISPLAY_NAME.match(p)]
        kql += "| where product in (" + ", ".join(f"'{p}'" for p in safe) + ")\n"
    kql += (
        "| summarize tokens = sum(Sum) by Name, product, model, app, "
        "day = format_datetime(startofday(TimeGenerated), 'yyyy-MM-dd')"
    )
    from citadel_agent.azure_clients.monitor import LogQuery

    result = LogQuery(ctx.runtime.credential).query_workspace(workspace, kql, timespan=timedelta(days=days))
    rows = []
    for row in result.rows:
        field = TOKEN_METRICS.get(str(row.get("Name")))
        if field:
            rows.append(
                {
                    "product": row.get("product") or "-",
                    "model": row.get("model") or "-",
                    "app": row.get("app") or "-",
                    "day": row.get("day") or "-",
                    "prompt": 0,
                    "completion": 0,
                    "cached": 0,
                    field: _int(row.get("tokens")),
                }
            )
    return rows


def _cost(row: dict[str, Any], entry: dict[str, Any] | None) -> float | None:
    if entry is None or str(entry.get("CalculationMethod", "tokens")).lower() != "tokens":
        return None
    unit = float(entry.get("CostUnit") or 1_000_000)
    input_price = float(entry.get("CostPerInputUnit") or 0)
    cached_price = float(entry.get("CostPerCachedInputUnit", input_price) or 0)
    output_price = float(entry.get("CostPerOutputUnit") or 0)
    uncached = max(row["prompt"] - row["cached"], 0)
    return (uncached * input_price + row["cached"] * cached_price + row["completion"] * output_price) / unit


def usage_report(
    ctx: ToolContext,
    environment: str | None = None,
    business_unit: str | None = None,
    use_case: str | None = None,
    days: int = 7,
    group_by: str = "model",
) -> str:
    if group_by not in ("model", "product", "app", "day"):
        raise ToolInputError("group_by must be model, product, app or day")
    env_name = ctx.environment_name(environment)
    env = ctx.environment(env_name)
    products = _scope_products(ctx, env_name, business_unit, use_case)
    if products == []:
        return "You do not own any access contract in this environment, so there is no usage to show."
    notes: list[str] = []
    pricing: dict[str, dict[str, Any]] = {}
    try:
        rows, pricing = _cosmos_usage(ctx, env, products, days)
        source = "Cosmos DB usage records"
    except Exception as exc:
        notes.append(f"Cosmos DB was not available. {diagnose(str(exc), 'Cosmos DB').summary}")
        rows = _metric_usage(ctx, env, products, days)
        source = "Application Insights token metrics (usage only, no cost)"
    totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"prompt": 0, "completion": 0, "cached": 0, "cost": 0.0}
    )
    priced_any = False
    unpriced: set[str] = set()
    currency = "USD"
    for row in rows:
        bucket = totals[row[group_by]]
        for field in ("prompt", "completion", "cached"):
            bucket[field] += row[field]
        entry = pricing.get(str(row["model"]).lower())
        cost = _cost(row, entry)
        if cost is None:
            unpriced.add(str(row["model"]))
        else:
            priced_any = True
            bucket["cost"] += cost
            currency = str(entry.get("Currency", currency)) if entry else currency
    if not rows:
        empty = f"No usage in the last {days} days ({source}). Usage data can lag by several minutes."
        return "\n".join([empty, *notes])
    header = f"| {group_by.capitalize()} | Prompt tokens | Cached | Completion tokens | Total |"
    separator = "|---|---|---|---|---|"
    if priced_any:
        header += f" Estimated cost ({currency}) |"
        separator += "---|"
    lines = [f"## Usage in {env_name}, last {days} days, by {group_by}", "", header, separator]
    grand = {"prompt": 0, "completion": 0, "cached": 0, "cost": 0.0}
    for key in sorted(totals, key=lambda k: -(totals[k]["prompt"] + totals[k]["completion"])):
        bucket = totals[key]
        for field in grand:
            grand[field] += bucket[field]
        line = (
            f"| {key} | {bucket['prompt']:,} | {bucket['cached']:,} | {bucket['completion']:,} "
            f"| {bucket['prompt'] + bucket['completion']:,} |"
        )
        lines.append(line + (f" {bucket['cost']:.4f} |" if priced_any else ""))
    total_line = (
        f"| Total | {grand['prompt']:,} | {grand['cached']:,} | {grand['completion']:,} "
        f"| {grand['prompt'] + grand['completion']:,} |"
    )
    lines.append(total_line + (f" {grand['cost']:.4f} |" if priced_any else ""))
    lines.append(f"\nSource: {source}. Data is ingested asynchronously and can lag by several minutes.")
    if source.startswith("Cosmos") and unpriced:
        lines.append(
            f"No active price in the model-pricing container for: {', '.join(sorted(unpriced))}. "
            "Costs are estimates from that table, not invoices."
        )
    lines.extend(notes)
    return "\n".join(lines)


def backend_contract_summary(ctx: ToolContext, environment: str | None = None) -> str:
    env_name = ctx.environment_name(environment)
    env = ctx.environment(env_name)
    contract = GatewayClient(env.gateway_url).backend_contract()
    operator = ctx.persona == "operator"
    lines = [
        f"## Backend contract in {env_name} (version {contract.backend_contract_version or '-'})",
        f"* Circuit breaker: {'on' if contract.circuit_breaker_enabled else 'off'}; "
        f"session affinity: {'on' if contract.session_affinity_enabled else 'off'}",
        "",
        "| Backend | Type | Models | Priority | Weight |" + (" Endpoint |" if operator else ""),
        "|---|---|---|---|---|" + ("---|" if operator else ""),
    ]
    for backend in contract.backends():
        models = ", ".join(
            str(m.get("name") if isinstance(m, dict) else m) for m in backend.get("supportedModels", [])
        )
        line = (
            f"| {backend.get('backendId')} | {backend.get('backendType')} | {models} "
            f"| {backend.get('priority', '-')} | {backend.get('weight', '-')} |"
        )
        lines.append(line + (f" {backend.get('endpoint', '-')} |" if operator else ""))
    pools = contract.pools()
    if pools:
        lines += ["", "| Pool | Type | Models |", "|---|---|---|"]
        lines += [
            f"| {p.get('poolName')} | {p.get('poolType', '-')} | {', '.join(p.get('supportedModels', []))} |"
            for p in pools
        ]
    aliases = contract.aliases()
    if aliases:
        lines += ["", "Aliases: " + ", ".join(str(a.get("name")) for a in aliases)]
    if contract.has_redacted_secrets():
        lines.append(
            "\nSome backends carry inline secrets (shown redacted by the gateway). Changes must start from "
            "the authoritative backend contract in the contracts repository, never from this live view."
        )
    return "\n".join(lines)


def _check(label: str, run: Any, resource: str = "the resource", roles: str | None = None) -> tuple[str, str]:
    try:
        detail = run()
    except KeyVaultAccessError as exc:
        text = str(exc)
        return ("blocked" if "network rules" in text else "missing role"), text
    except Exception as exc:
        diagnosis = diagnose(str(exc), resource, roles)
        status = {"firewall": "blocked", "rbac": "missing role", "unreachable": "unreachable"}
        return status.get(diagnosis.kind, "error"), diagnosis.summary
    return "ok", detail or label


def preflight(ctx: ToolContext, environment: str | None = None) -> str:
    env_name = ctx.environment_name(environment)
    env = ctx.environment(env_name)
    runtime = ctx.runtime
    catalogs = load_catalogs()
    rows: list[tuple[str, str, str]] = []

    def roles(capability: str) -> str:
        entry = catalogs.capability(capability)
        return ", ".join(entry.roles) if entry and entry.roles else "-"

    status, detail = _check(
        "gateway reachable",
        lambda: f"version {GatewayClient(env.gateway_url).version().get('master-version', '?')}",
    )
    rows.append(("Gateway", status, detail))
    can_deploy = runtime.can_deploy(env)
    rows.append(
        (
            "Deploy contracts directly",
            "ok" if can_deploy else "missing role",
            f"mode {env.mode.value}"
            if can_deploy
            else f"needs {roles('deploy-access-contract')} (pull requests still work)",
        )
    )
    if env.key_vault is not None:
        reader = KeyVaultReader(runtime.credential, vault_url(env.key_vault.name))
        status, detail = _check(
            "secrets readable", lambda: f"{len(reader.list_secret_names())} secrets visible"
        )
        rows.append(
            (
                f"Key Vault {env.key_vault.name}",
                status,
                detail if status == "ok" else f"{detail} Needs {roles('read-key-vault-secrets')}.",
            )
        )
    if env.cosmos is not None:
        cosmos = CosmosUsageReader(runtime.credential, cosmos_endpoint(env.cosmos.account_name))
        status, detail = _check(
            "usage readable",
            lambda: f"databases: {', '.join(cosmos.list_databases())}",
            f"Cosmos DB {env.cosmos.account_name}",
            roles("read-usage-cosmos"),
        )
        rows.append((f"Cosmos DB {env.cosmos.account_name}", status, detail))
    if env.log_analytics_workspace_id:
        from citadel_agent.azure_clients.monitor import LogQuery

        query = LogQuery(runtime.credential)
        workspace = env.log_analytics_workspace_id

        def sample_logs() -> str:
            result = query.query_workspace(workspace, "AppMetrics | take 1", timespan=timedelta(hours=1))
            return f"{len(result.rows)} sample rows"

        status, detail = _check("logs readable", sample_logs, "Log Analytics", roles("read-logs"))
        rows.append(("Log Analytics", status, detail))
    provider = runtime.git_provider()
    rows.append(
        (
            "Pull requests",
            "ok" if provider is not None else "not configured",
            runtime.policy.gitops.repository
            if provider is not None and runtime.policy
            else "Sign in with gh auth login or set GITHUB_TOKEN",
        )
    )
    lines = [
        f"## Preflight for {env_name} ({runtime.principal.label}, {ctx.persona})",
        "",
        "| Check | Status | Detail |",
        "|---|---|---|",
    ]
    lines += [f"| {check} | {status} | {detail} |" for check, status, detail in rows]
    return "\n".join(lines)


def register_operations_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    def usage_report_tool(
        environment: EnvironmentName = None,
        business_unit: Annotated[str | None, Field(description="Business unit of the use case.")] = None,
        use_case: Annotated[str | None, Field(description="Use case name.")] = None,
        days: Annotated[int, Field(ge=1, le=90, description="Number of days to report.")] = 7,
        group_by: Annotated[GroupBy, Field(description="Group by model, product, app or day.")] = "model",
    ) -> str:
        return usage_report(ctx, environment, business_unit, use_case, days, group_by)

    def backend_contract_summary_tool(environment: EnvironmentName = None) -> str:
        return backend_contract_summary(ctx, environment)

    def preflight_tool(environment: EnvironmentName = None) -> str:
        return preflight(ctx, environment)

    registry.add(
        ToolSpec(
            "usage_report",
            usage_report_tool,
            "Token usage and estimated cost by model, product, app or day (consumers see their own).",
        )
    )
    registry.add(
        ToolSpec(
            "backend_contract_summary",
            backend_contract_summary_tool,
            "Live model backends, pools, aliases, circuit breaker and session affinity of the gateway.",
        )
    )
    registry.add(
        ToolSpec(
            "preflight",
            preflight_tool,
            "What the caller can do here: roles and network reachability of Key Vault, Cosmos DB and logs.",
        )
    )
