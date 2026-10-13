# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Backend onboarding tool (platform operators): change the gateway's backend contract.

Deploying the backend contract replaces the whole backend configuration, so every change starts
from the authoritative backend intent in the contracts repository (imported once from the live
gateway when it holds no inline secrets) and merges into it. (No postponed annotations.)
"""

from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError

from citadel_agent.contracts.backend import BackendContractIntent
from citadel_agent.contracts.common import ContractMetadata
from citadel_agent.gateway.client import GatewayClient
from citadel_agent.policy.admin import Operation
from citadel_agent.tools.context import ToolContext
from citadel_agent.tools.registry import ToolInputError, ToolRegistry, ToolSpec

IDENTITY_API_VERSION = "2023-01-31"
ROLE_ASSIGNMENTS_API_VERSION = "2022-04-01"
# Built-in roles that let the gateway's managed identity call models on a Foundry or OpenAI account.
INFERENCE_ROLE_IDS = {
    "a97b65f3-24c7-4388-baec-2e87135dc908": "Cognitive Services User",
    "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd": "Cognitive Services OpenAI User",
}
OPERATOR_ONLY = frozenset({"operator"})


def current_backend_intent(ctx: ToolContext, env_name: str) -> tuple[BackendContractIntent, str]:
    """The authoritative backend intent and where it came from."""
    for record in ctx.repository().intents("backend-contract"):
        if record.intent.environment == env_name:
            return record.intent, f"the contracts repository ({record.path})"
    env = ctx.environment(env_name)
    live = GatewayClient(env.gateway_url).backend_contract()
    if live.has_redacted_secrets():
        raise ToolInputError(
            "The live backend contract holds inline secrets (shown redacted), so it cannot be imported. "
            "Add the authoritative backend contract (from the original LLM_BACKEND_CONFIG) to the repository."
        )
    raw = live.raw
    try:
        intent = BackendContractIntent.model_validate(
            {
                "environment": env_name,
                "metadata": ContractMetadata(
                    owners=[ctx.caller], description="Imported from the live gateway"
                ).model_dump(by_alias=True),
                "backends": live.backends(),
                "modelAliases": live.aliases(),
                "circuitBreakerEnabled": live.circuit_breaker_enabled,
                "circuitBreakerDefaults": (raw.get("circuitBreaker") or {}).get("defaults"),
                "sessionAffinityEnabled": live.session_affinity_enabled,
                "sessionAffinityDefaults": (raw.get("sessionAffinity") or {}).get("defaults"),
            }
        )
    except ValidationError as exc:
        raise ToolInputError(f"The live backend contract could not be imported: {exc.errors()[:3]}") from exc
    return intent, "the live gateway (first import; it becomes the authoritative contract)"


def _account_backend(
    ctx: ToolContext, subscription_id: str, account_name: str, models: list[str] | None
) -> tuple[dict[str, Any], str]:
    accounts = ctx.runtime.arm.list_cognitive_accounts(subscription_id)
    account = next((a for a in accounts if a.get("name", "").lower() == account_name.lower()), None)
    if account is None:
        raise ToolInputError(
            f"No Foundry or Azure OpenAI account named {account_name} in subscription {subscription_id}"
        )
    deployments = ctx.runtime.arm.list_cognitive_deployments(account["id"])
    entries = []
    for deployment in deployments:
        if models and deployment["name"] not in models:
            continue
        model = (deployment.get("properties") or {}).get("model") or {}
        sku = deployment.get("sku") or {}
        entry = {"name": deployment["name"], "sku": sku.get("name"), "capacity": sku.get("capacity")}
        entry.update({"modelFormat": model.get("format"), "modelVersion": model.get("version")})
        entries.append({k: v for k, v in entry.items() if v is not None})
    if not entries:
        raise ToolInputError(f"{account_name} has no matching model deployments")
    backend = {
        "backendId": account["name"],
        "backendType": "azure-openai" if account.get("kind") == "OpenAI" else "ai-foundry",
        "endpoint": (account.get("properties") or {}).get("endpoint"),
        "authType": "managed-identity",
        "supportedModels": entries,
    }
    return backend, account["id"]


def _missing_role_prerequisite(
    ctx: ToolContext, env: Any, account_id: str, account_name: str
) -> dict[str, Any] | None:
    """A manual step granting the gateway identity an inference role, when it does not have one."""
    identity = env.apim_managed_identity
    if identity is None:
        return None
    arm = ctx.runtime.arm
    identity_id = (
        f"/subscriptions/{identity.subscription_id}/resourceGroups/{identity.resource_group_name}"
        f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{identity.name}"
    )
    principal = arm.get(identity_id, IDENTITY_API_VERSION)["properties"]["principalId"]
    assignments = arm.list(
        f"{account_id}/providers/Microsoft.Authorization/roleAssignments",
        ROLE_ASSIGNMENTS_API_VERSION,
        {"$filter": f"assignedTo('{principal}')"},
    )
    if any(a["properties"]["roleDefinitionId"].rsplit("/", 1)[-1] in INFERENCE_ROLE_IDS for a in assignments):
        return None
    return {
        "stepId": f"rbac-{account_name.lower()}"[:40],
        "description": (
            f"Grant the gateway identity {identity.name} the Cognitive Services User role on {account_name}"
        ),
        "commands": [
            f"az role assignment create --assignee-object-id {principal} --assignee-principal-type "
            f'ServicePrincipal --role "Cognitive Services User" --scope {account_id}'
        ],
        "verification": f"az role assignment list --assignee {principal} --scope {account_id} -o table",
    }


def _served_models(intent: BackendContractIntent) -> set[str]:
    served = {m.name.lower() for b in intent.backends for m in b.supported_models}
    return served | {a.name.lower() for a in intent.model_aliases}


def draft_backend_change(
    ctx: ToolContext,
    environment: str | None = None,
    add_account: str | None = None,
    account_subscription_id: str | None = None,
    models: list[str] | None = None,
    priority: int | None = None,
    weight: int | None = None,
    remove_backend: str | None = None,
    alias_name: str | None = None,
    alias_models: list[str] | None = None,
    alias_strategy: str | None = None,
    circuit_breaker_enabled: bool | None = None,
    session_affinity_enabled: bool | None = None,
    draft_id: str | None = None,
) -> str:
    env_name = ctx.environment_name(environment)
    env = ctx.environment(env_name)
    ctx.runtime.authorizer.require(Operation.BACKEND_WRITE)
    before, source = current_backend_intent(ctx, env_name)
    data = before.model_dump(mode="json", by_alias=True, exclude_none=True)
    changes: list[str] = []
    prerequisites: list[dict[str, Any]] = []
    if add_account:
        backend, account_id = _account_backend(
            ctx, account_subscription_id or env.subscription_id, add_account, models
        )
        existing = next(
            (b for b in data["backends"] if b["backendId"].lower() == backend["backendId"].lower()), None
        )
        for key, value in (("priority", priority), ("weight", weight)):
            if value is not None:
                backend[key] = value
            elif existing and key in existing:
                backend[key] = existing[key]
        model_names = ", ".join(m["name"] for m in backend["supportedModels"])
        if existing:
            existing.update(backend)
            changes.append(f"Updated backend {backend['backendId']}: {model_names}")
        else:
            data["backends"].append(backend)
            changes.append(f"Added backend {backend['backendId']}: {model_names}")
        prerequisite = _missing_role_prerequisite(ctx, env, account_id, backend["backendId"])
        if prerequisite:
            prerequisites.append(prerequisite)
    if remove_backend:
        remaining = [b for b in data["backends"] if b["backendId"].lower() != remove_backend.lower()]
        if len(remaining) == len(data["backends"]):
            raise ToolInputError(f"No backend named {remove_backend}")
        data["backends"] = remaining
        changes.append(f"Removed backend {remove_backend}")
    if alias_name:
        aliases = [a for a in data.get("modelAliases", []) if a["name"].lower() != alias_name.lower()]
        if alias_models:
            aliases.append(
                {"name": alias_name, "models": alias_models, "strategy": alias_strategy or "priority"}
            )
            changes.append(f"Alias {alias_name} -> {', '.join(alias_models)}")
        else:
            changes.append(f"Removed alias {alias_name}")
        data["modelAliases"] = aliases
    if circuit_breaker_enabled is not None:
        data["circuitBreakerEnabled"] = circuit_breaker_enabled
        changes.append(f"Circuit breaker {'on' if circuit_breaker_enabled else 'off'}")
    if session_affinity_enabled is not None:
        data["sessionAffinityEnabled"] = session_affinity_enabled
        changes.append(f"Session affinity {'on' if session_affinity_enabled else 'off'}")
    if not changes and source.startswith("the contracts repository"):
        raise ToolInputError("Nothing to change: name an account to add, a backend to remove or an alias")
    try:
        intent = BackendContractIntent.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
        raise ToolInputError(f"The backend contract is not valid: {details}") from exc
    lost = sorted(_served_models(before) - _served_models(intent))
    warnings = []
    if lost:
        affected = [
            f"{r.intent.use_case.business_unit}/{r.intent.use_case.use_case_name}"
            for r in ctx.access_contracts(env_name)
            if any(m.lower() in lost for s in r.intent.services for m in s.policy.allowed_models)
        ]
        warnings.append(
            f"These models would no longer be served: {', '.join(lost)}."
            + (f" Access contracts that allow them: {', '.join(affected)}." if affected else "")
            + " Confirm the impact with the user before preparing."
        )
    draft = ctx.drafts.save(
        intent,
        operation=Operation.BACKEND_WRITE,
        created_by=ctx.caller,
        step_id=f"backend-{env_name}",
        draft_id=draft_id,
        prerequisites=tuple(prerequisites),
    )
    lines = [f"Backend contract change drafted (draft_id: {draft.id}), starting from {source}."]
    lines += [f"* {change}" for change in changes] or [
        "* No changes; the draft records the imported contract."
    ]
    lines.append(
        f"Result: {len(intent.backends)} backends serving {len(_served_models(intent))} models and aliases; "
        f"circuit breaker {'on' if intent.circuit_breaker_enabled else 'off'}, "
        f"session affinity {'on' if intent.session_affinity_enabled else 'off'}."
    )
    for prerequisite in prerequisites:
        lines.append(f"* Prerequisite (manual): {prerequisite['description']}")
    lines.extend(f"Warning: {warning}" for warning in warnings)
    lines.append(
        "Nothing is deployed yet. Review with the user, then call prepare_change with this draft_id."
    )
    return "\n".join(lines)


def register_backend_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    def draft_backend_change_tool(
        environment: Annotated[
            str | None, Field(description="Environment; defaults to the current one.")
        ] = None,
        add_account: Annotated[
            str | None, Field(description="Foundry or Azure OpenAI account to add or refresh as a backend.")
        ] = None,
        account_subscription_id: Annotated[
            str | None, Field(description="Subscription of that account.")
        ] = None,
        models: Annotated[
            list[str] | None, Field(description="Deployments to include (default: all).")
        ] = None,
        priority: Annotated[
            int | None, Field(ge=1, le=5, description="Routing priority (1 is highest).")
        ] = None,
        weight: Annotated[int | None, Field(ge=1, le=1000, description="Weight within a priority.")] = None,
        remove_backend: Annotated[str | None, Field(description="Backend ID to remove.")] = None,
        alias_name: Annotated[str | None, Field(description="Model alias to add, change or remove.")] = None,
        alias_models: Annotated[
            list[str] | None, Field(description="Alias members; empty removes the alias.")
        ] = None,
        alias_strategy: Annotated[
            Literal["priority", "weighted"] | None, Field(description="Alias routing.")
        ] = None,
        circuit_breaker_enabled: Annotated[
            bool | None, Field(description="Turn the circuit breaker on or off.")
        ] = None,
        session_affinity_enabled: Annotated[
            bool | None, Field(description="Turn session affinity on or off.")
        ] = None,
        draft_id: Annotated[
            str | None, Field(description="Revise this draft instead of creating a new one.")
        ] = None,
    ) -> str:
        return draft_backend_change(
            ctx,
            environment,
            add_account,
            account_subscription_id,
            models,
            priority,
            weight,
            remove_backend,
            alias_name,
            alias_models,
            alias_strategy,
            circuit_breaker_enabled,
            session_affinity_enabled,
            draft_id,
        )

    registry.add(
        ToolSpec(
            "draft_backend_change",
            draft_backend_change_tool,
            "Draft a change to the gateway's model backends, aliases, circuit breaker or session affinity "
            "(merged into the authoritative backend contract). Changes nothing until prepared and approved.",
            personas=OPERATOR_ONLY,
        )
    )
