# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Discovery tools: which models exist and how to call them.

(This module deliberately does not use postponed annotations; see ``tools.core``.)
"""

from typing import Annotated, Any

from pydantic import Field

from citadel_agent.codegen import FRAMEWORKS, CodegenError, CodeRequest, generate, model_kind
from citadel_agent.contracts.access import AccessContractIntent
from citadel_agent.gateway.client import GatewayClient
from citadel_agent.tools.context import ToolContext
from citadel_agent.tools.registry import ToolInputError, ToolRegistry, ToolSpec

BusinessUnit = Annotated[str | None, Field(description="Business unit of the use case, for example HR.")]
UseCase = Annotated[str | None, Field(description="Use case name, for example ChatBot.")]
EnvironmentName = Annotated[str | None, Field(description="Environment name; defaults to the current one.")]
Framework = Annotated[str, Field(description=f"One of: {', '.join(FRAMEWORKS)}.")]
ModelName = Annotated[str, Field(description="Model or alias name, for example gpt-5.2.")]


def _contract(ctx: ToolContext, business_unit: str | None, use_case: str | None, environment: str) -> Any:
    if not business_unit or not use_case:
        return None
    record = ctx.find_access_contract(business_unit, use_case, environment)
    if record is None:
        raise ToolInputError(
            f"No access contract for {business_unit}/{use_case} in {environment}. "
            "Use list_access_contracts, or onboard the use case first."
        )
    return record.intent


def _allowed_models(intent: AccessContractIntent) -> set[str] | None:
    """Models a contract allows (lowercase), or None when any model is allowed."""
    allowed: set[str] = set()
    for service in intent.services:
        if not service.policy.allowed_models:
            return None
        allowed.update(model.lower() for model in service.policy.allowed_models)
    return allowed


def list_models(
    ctx: ToolContext,
    environment: str | None = None,
    business_unit: str | None = None,
    use_case: str | None = None,
) -> str:
    env_name = ctx.environment_name(environment)
    env = ctx.environment(env_name)
    contract = GatewayClient(env.gateway_url).backend_contract()
    models: dict[str, dict[str, Any]] = {}
    for backend in contract.backends():
        for entry in backend.get("supportedModels", []):
            info = entry if isinstance(entry, dict) else {"name": entry}
            name = info.get("name")
            if not name:
                continue
            row = models.setdefault(
                name, {"backends": [], "capacity": 0, "retires": info.get("retirementDate"), "version": None}
            )
            row["backends"].append(f"{backend.get('backendId')} ({backend.get('backendType')})")
            row["capacity"] += int(info.get("capacity") or 0)
            row["version"] = info.get("modelVersion") or row["version"]
    aliases = {a["name"]: a for a in contract.aliases() if "name" in a}
    intent = _contract(ctx, business_unit, use_case, env_name)
    allowed = _allowed_models(intent) if intent is not None else None
    header = f"## Models in {env_name}"
    if intent is not None:
        header += f" for {intent.use_case.business_unit}/{intent.use_case.use_case_name}"
        header += " (all models are allowed)" if allowed is None else ""
    lines = [
        header,
        "",
        "| Model | Type | Version | Backends | Capacity | Retires |",
        "|---|---|---|---|---|---|",
    ]
    for name in sorted(models, key=str.lower):
        if allowed is not None and name.lower() not in allowed:
            continue
        row = models[name]
        lines.append(
            f"| {name} | {model_kind(name)} | {row['version'] or '-'} | {', '.join(row['backends'])} "
            f"| {row['capacity'] or '-'} | {row['retires'] or '-'} |"
        )
    for name in sorted(aliases):
        if allowed is None or name.lower() in allowed:
            lines.append(
                f"| {name} | alias | - | {aliases[name].get('description', 'model alias')} | - | - |"
            )
    if allowed is not None:
        missing = sorted(allowed - {m.lower() for m in models} - {a.lower() for a in aliases})
        if missing:
            lines.append(f"\nAllowed by the contract but not onboarded on the gateway: {', '.join(missing)}")
    lines.append(
        "\nCapacity is the deployed capacity across backends: thousands of tokens per minute for Standard "
        "deployments, provisioned throughput units for provisioned ones."
    )
    return "\n".join(lines)


def generate_client_code(
    ctx: ToolContext,
    framework: str,
    model: str,
    business_unit: str | None = None,
    use_case: str | None = None,
    environment: str | None = None,
) -> str:
    env_name = ctx.environment_name(environment)
    env = ctx.environment(env_name)
    intent = _contract(ctx, business_unit, use_case, env_name)
    notes: list[str] = []
    key_secret = "<key-secret-name>"  # noqa: S105 - a placeholder the user replaces, not a secret
    jwt_scope = None
    key_vault = env.key_vault.name if env.key_vault else None
    if intent is None:
        notes.append("No use case given: replace <key-secret-name> with your access contract's key secret.")
    else:
        service = intent.services[0]
        key_secret = service.key_secret_name.lower().replace("_", "-")
        allowed = _allowed_models(intent)
        if allowed is not None and model.lower() not in allowed:
            notes.append(
                f"{model} is not allowed by this contract; calls will fail with 403 until it is added."
            )
        if service.policy.jwt.required:
            jwt_scope = "api://<gateway-app-id>/.default"
            notes.append(
                "The contract requires a JWT: replace <gateway-app-id> with the gateway app registration."
            )
        if framework == "azure-openai-python" and "azure-openai-api" not in service.apis:
            notes.append(
                "The contract does not include azure-openai-api; use an OpenAI-compatible framework."
            )
    try:
        code = generate(
            CodeRequest(
                framework=framework,
                gateway_url=env.gateway_url,
                model=model,
                key_secret=key_secret,
                key_vault=key_vault,
                jwt_scope=jwt_scope,
            )
        )
    except CodegenError as exc:
        raise ToolInputError(str(exc)) from exc
    parts = [f"```{code.language}\n{code.code}\n```"]
    if code.packages:
        parts.append(f"Requirements: {', '.join(code.packages)}.")
    parts.append(
        "The key is read at runtime from Key Vault (the caller needs the Key Vault Secrets User role) "
        "or from the CITADEL_API_KEY environment variable. Never paste it into code."
    )
    parts.extend(f"Note: {note}" for note in (*code.notes, *notes))
    return "\n\n".join(parts)


def register_discovery_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    def list_models_tool(
        environment: EnvironmentName = None, business_unit: BusinessUnit = None, use_case: UseCase = None
    ) -> str:
        return list_models(ctx, environment, business_unit, use_case)

    def generate_client_code_tool(
        framework: Framework,
        model: ModelName,
        business_unit: BusinessUnit = None,
        use_case: UseCase = None,
        environment: EnvironmentName = None,
    ) -> str:
        return generate_client_code(ctx, framework, model, business_unit, use_case, environment)

    registry.add(
        ToolSpec(
            "list_models",
            list_models_tool,
            "Models and aliases on the gateway, optionally limited to what one use case's contract allows.",
        )
    )
    registry.add(
        ToolSpec(
            "generate_client_code",
            generate_client_code_tool,
            "Working client code for a model and framework, with the key read from Key Vault at runtime.",
        )
    )
