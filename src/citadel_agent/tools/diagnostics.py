# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Diagnostic tools: explain errors, read gateway logs and smoke-test a contract.

Consumers only see logs and test calls for contracts they own; operators see everything.
KQL is built from validated values only. (No postponed annotations; see ``tools.core``.)
"""

import re
from datetime import timedelta
from typing import Annotated, Any

from pydantic import Field

from citadel_agent.azure_clients.keyvault import KeyVaultAccessError, KeyVaultReader, vault_url
from citadel_agent.codegen import model_kind
from citadel_agent.gateway.client import GatewayClient
from citadel_agent.knowledge.catalogs import load_catalogs
from citadel_agent.tools.context import ToolContext
from citadel_agent.tools.registry import ToolInputError, ToolRegistry, ToolSpec

_GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_PRODUCT = re.compile(r"^[A-Za-z0-9-]{1,80}$")
LOG_COLUMNS = (
    "TimeGenerated, CorrelationId, Method, Url, ResponseCode, ProductId, BackendId, TotalTime, "
    "LastErrorSource, LastErrorSection, LastErrorReason, LastErrorMessage"
)

BusinessUnit = Annotated[str | None, Field(description="Business unit of the use case.")]
UseCase = Annotated[str | None, Field(description="Use case name.")]
EnvironmentName = Annotated[str | None, Field(description="Environment name; defaults to the current one.")]


def _visible_products(ctx: ToolContext, env_name: str) -> list[str] | None:
    """Product IDs the caller may inspect, or None for all (operators)."""
    if ctx.persona == "operator":
        return None
    authorizer = ctx.runtime.authorizer
    return [
        record.intent.product_id(service.code)
        for record in ctx.access_contracts(env_name)
        if authorizer.owns(record.intent)
        for service in record.intent.services
    ]


def _query_logs(ctx: ToolContext, env_name: str, kql: str, minutes: int) -> list[dict[str, Any]]:
    env = ctx.environment(env_name)
    workspace = env.log_analytics_workspace_id
    if not workspace:
        raise ToolInputError(f"No Log Analytics workspace is configured for {env_name}")
    from citadel_agent.azure_clients.monitor import LogQuery

    result = LogQuery(ctx.runtime.credential).query_workspace(
        workspace, kql, timespan=timedelta(minutes=minutes)
    )
    return list(result.rows)


def _format_logs(rows: list[dict[str, Any]]) -> str:
    lines = ["| Time | Status | Request ID | Product | URL | Error |", "|---|---|---|---|---|---|"]
    for row in rows:
        error = " / ".join(
            str(row.get(key))
            for key in ("LastErrorSource", "LastErrorReason", "LastErrorMessage")
            if row.get(key)
        )
        url = str(row.get("Url", "")).split("?", 1)[0]
        time_generated = str(row.get("TimeGenerated", ""))[:19]
        lines.append(
            f"| {time_generated} | {row.get('ResponseCode')} | {row.get('CorrelationId')} "
            f"| {row.get('ProductId') or '-'} | {url} | {error or '-'} |"
        )
    return "\n".join(lines)


def gateway_logs(
    ctx: ToolContext,
    request_id: str | None = None,
    environment: str | None = None,
    business_unit: str | None = None,
    use_case: str | None = None,
    minutes: int = 60,
    errors_only: bool = True,
) -> str:
    env_name = ctx.environment_name(environment)
    visible = _visible_products(ctx, env_name)
    filters = []
    if request_id:
        if not _GUID.match(request_id):
            raise ToolInputError("A request ID looks like 0f8fad5b-d9cb-469f-a165-70867728950e")
        filters.append(f"CorrelationId == '{request_id.lower()}'")
    if business_unit and use_case:
        intent = ctx.owned_access_contract(business_unit, use_case, env_name)
        products = [intent.product_id(s.code) for s in intent.services]
    else:
        products = visible
    if products is not None:
        if not products:
            return "You do not own any access contract in this environment, so there are no logs to show."
        safe = [p for p in products if _PRODUCT.match(p)]
        filters.append("ProductId in (" + ", ".join(f"'{p}'" for p in safe) + ")")
    if errors_only and not request_id:
        filters.append("ResponseCode >= 400")
    kql = "ApiManagementGatewayLogs"
    for condition in filters:
        kql += f"\n| where {condition}"
    kql += f"\n| project {LOG_COLUMNS}\n| order by TimeGenerated desc\n| take 25"
    rows = _query_logs(ctx, env_name, kql, minutes if not request_id else max(minutes, 1440))
    if not rows:
        return (
            "No matching gateway log entries. Logs arrive two to five minutes after a call; try again "
            "shortly or widen the time range."
        )
    return _format_logs(rows)


def explain_error(
    ctx: ToolContext,
    status: int | None = None,
    error_body: str | None = None,
    response_headers: list[str] | None = None,
    request_id: str | None = None,
    surface: str = "llm",
    environment: str | None = None,
) -> str:
    catalogs = load_catalogs()
    log_message = None
    log_text = ""
    if request_id and _GUID.match(request_id):
        try:
            rows = _query_logs(
                ctx,
                ctx.environment_name(environment),
                f"ApiManagementGatewayLogs\n| where CorrelationId == '{request_id.lower()}'\n"
                f"| project {LOG_COLUMNS}",
                1440,
            )
        except Exception as exc:
            log_text = f"\nThe gateway log could not be read: {exc}"
        else:
            visible = _visible_products(ctx, ctx.environment_name(environment))
            rows = [r for r in rows if visible is None or r.get("ProductId") in visible]
            if rows:
                log_message = " ".join(
                    str(rows[0].get(k) or "")
                    for k in ("LastErrorSource", "LastErrorReason", "LastErrorMessage")
                )
                status = status or int(rows[0].get("ResponseCode") or 0) or None
                log_text = "\nGateway log for this request:\n" + _format_logs(rows[:1])
            else:
                log_text = "\nNo gateway log entry yet for this request ID (logs can take a few minutes)."
    headers = {name.strip(): "" for name in (response_headers or []) if name.strip()}
    matches = catalogs.match_errors(
        status=status, body=error_body, headers=headers, log_message=log_message, surface=surface
    )
    if not matches:
        return (
            f"No known Citadel error matches status {status} and this body. Use gateway_logs with the "
            "request ID, or search_docs." + log_text
        )
    lines = []
    for entry in matches[:2]:
        lines += [
            f"## {entry.title} ({entry.status or '-'}{', ' + ', '.join(entry.codes) if entry.codes else ''})",
            f"* Who can fix it: {entry.owner.replace('-', ' ')}",
            *[f"* Cause: {cause}" for cause in entry.causes],
            *[f"* Fix: {fix}" for fix in entry.fixes],
        ]
        if entry.checks:
            lines.append(f"* Checks: {', '.join(entry.checks)}")
        if entry.docs:
            lines.append(f"* Documentation: {', '.join(entry.docs)}")
    return "\n".join(lines) + log_text


def smoke_test(
    ctx: ToolContext,
    business_unit: str,
    use_case: str,
    environment: str | None = None,
    model: str | None = None,
) -> str:
    env_name = ctx.environment_name(environment)
    env = ctx.environment(env_name)
    intent = ctx.owned_access_contract(business_unit, use_case, env_name)
    service = intent.services[0]
    if service.policy.jwt.required:
        raise ToolInputError(
            "This contract requires a JWT; the smoke test supports subscription-key contracts only"
        )
    if env.key_vault is None or not intent.store_credentials_in_key_vault:
        raise ToolInputError("The contract's key is not stored in a Key Vault the agent knows")
    model = model or (service.policy.allowed_models[0] if service.policy.allowed_models else None)
    if not model:
        raise ToolInputError("Name the model to test")
    reader = KeyVaultReader(ctx.runtime.credential, vault_url(env.key_vault.name))
    try:
        key = reader.get_secret_in_memory(service.key_secret_name.lower().replace("_", "-"))
    except KeyVaultAccessError as exc:
        raise ToolInputError(str(exc)) from exc
    client = GatewayClient(env.gateway_url)
    try:
        if model_kind(model) == "embeddings":
            result = client.embeddings_smoke_test(key, model)
        else:
            result = client.chat_smoke_test(key, model)
    finally:
        client.close()
    info = result.info
    verdict = "passed" if result.ok else "failed"
    lines = [
        f"## Smoke test of {intent.product_id(service.code)} with {model}: {verdict}",
        f"* Status {info.status_code} in {info.latency_ms:.0f} ms, region {info.region or '-'}",
    ]
    if info.request_id:
        lines.append(f"* Gateway request ID: {info.request_id}")
    if info.backend_request_id:
        lines.append(f"* Model backend request ID: {info.backend_request_id}")
    if info.remaining_tokens:
        lines.append(f"* Remaining tokens this minute: {info.remaining_tokens}")
    if result.usage:
        lines.append(f"* Tokens used: {result.usage.get('total_tokens', result.usage)}")
    if result.preview:
        lines.append(f"* Model replied: {result.preview}")
    if info.error:
        lines.append(f"* Error: {info.error.code or ''} {info.error.message}".rstrip())
        lines.append("Use explain_error with this status, error and request ID for the cause and fix.")
    return "\n".join(lines)


def register_diagnostic_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    def explain_error_tool(
        status: Annotated[
            int | None, Field(ge=100, le=599, description="HTTP status code of the failed call.")
        ] = None,
        error_body: Annotated[
            str | None, Field(description="The error response body, without keys or tokens.")
        ] = None,
        response_headers: Annotated[
            list[str] | None, Field(description="Names of the response headers (values are not needed).")
        ] = None,
        request_id: Annotated[
            str | None,
            Field(description="Gateway request ID: activityId from the error body or UAIG-Request-Id."),
        ] = None,
        surface: Annotated[str, Field(description="llm, mcp, a2a, azure or entra.")] = "llm",
        environment: EnvironmentName = None,
    ) -> str:
        return explain_error(ctx, status, error_body, response_headers, request_id, surface, environment)

    def gateway_logs_tool(
        request_id: Annotated[str | None, Field(description="Request ID to look up.")] = None,
        environment: EnvironmentName = None,
        business_unit: BusinessUnit = None,
        use_case: UseCase = None,
        minutes: Annotated[int, Field(ge=5, le=10080, description="How far back to look.")] = 60,
        errors_only: Annotated[bool, Field(description="Only failed requests.")] = True,
    ) -> str:
        return gateway_logs(ctx, request_id, environment, business_unit, use_case, minutes, errors_only)

    def smoke_test_tool(
        business_unit: Annotated[str, Field(description="Business unit of the use case.")],
        use_case: Annotated[str, Field(description="Use case name.")],
        environment: EnvironmentName = None,
        model: Annotated[
            str | None, Field(description="Model to call; defaults to the first allowed.")
        ] = None,
    ) -> str:
        return smoke_test(ctx, business_unit, use_case, environment, model)

    registry.add(
        ToolSpec(
            "explain_error",
            explain_error_tool,
            "Explain a failed gateway call from its status, error body and request ID, with cause and fix.",
        )
    )
    registry.add(
        ToolSpec("gateway_logs", gateway_logs_tool, "Recent gateway log entries (failures by default).")
    )
    registry.add(
        ToolSpec(
            "smoke_test",
            smoke_test_tool,
            "Make one small real call with a contract's own key to prove access works. Spends a few tokens.",
        )
    )
