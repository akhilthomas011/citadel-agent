# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Access contract tools: inventory, description and drafting (onboarding and changes).

(This module deliberately does not use postponed annotations; see ``tools.core``.)
"""

from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError

from citadel_agent.contracts.access import AccessContractIntent
from citadel_agent.contracts.intent_io import parse_intent
from citadel_agent.policy.admin import Operation
from citadel_agent.tools.context import ToolContext
from citadel_agent.tools.registry import ToolInputError, ToolRegistry, ToolSpec

DEFAULT_APIS = ["unified-ai-api", "universal-llm-api"]
SERVICE_CODE = "LLM"
QUOTA_PERIOD_WORDS = {"Monthly": "month", "Weekly": "week"}

BusinessUnit = Annotated[
    str, Field(description="Business unit, letters, digits and hyphens (for example HR).")
]
UseCase = Annotated[
    str, Field(description="Use case name, letters, digits and hyphens (for example ChatBot).")
]
EnvironmentName = Annotated[str | None, Field(description="Environment name; defaults to the current one.")]
OptionalModels = Annotated[
    list[str] | None, Field(description="Models the use case may call (replaces the list).")
]
PiiMode = Annotated[
    Literal["off", "anonymize", "block"] | None,
    Field(description="PII handling: off, anonymize (mask before the model sees it) or block (reject)."),
]


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()[:8]
    )


def describe_intent(intent: AccessContractIntent, key_vault: str | None) -> str:
    use = intent.use_case
    lines = [f"## {use.business_unit}/{use.use_case_name} in {intent.environment}"]
    if intent.metadata.description:
        lines.append(intent.metadata.description)
    lines.append(f"* Owners: {', '.join(intent.metadata.owners + intent.metadata.owner_groups) or 'none'}")
    for service in intent.services:
        policy = service.policy
        product = intent.product_id(service.code)
        lines += [
            f"* Product `{product}` with subscription `{intent.subscription_name(service.code)}`, "
            f"APIs: {', '.join(service.apis)}",
            f"  * Key: Key Vault secret `{service.key_secret_name.lower().replace('_', '-')}`"
            + (f" in vault `{key_vault}`" if key_vault and intent.store_credentials_in_key_vault else ""),
            f"  * Models: {', '.join(policy.allowed_models) or 'all models'}",
        ]
        if service.endpoint_secret_name:
            lines.append(
                f"  * Endpoint: Key Vault secret `{service.endpoint_secret_name.lower().replace('_', '-')}`"
            )
        if policy.token_limit:
            limit = policy.token_limit
            quota = (
                f", quota {limit.token_quota:,} tokens per "
                f"{QUOTA_PERIOD_WORDS.get(limit.token_quota_period, limit.token_quota_period)}"
                if limit.token_quota and limit.token_quota_period
                else ""
            )
            lines.append(f"  * Limits: {limit.tokens_per_minute:,} tokens per minute{quota}")
        for model_limit in policy.model_token_limits:
            lines.append(f"  * {model_limit.model}: {model_limit.tokens_per_minute:,} tokens per minute")
        safety = [
            f"content safety {'on' if policy.content_safety.enabled else 'off'}",
            f"PII {policy.pii.mode}",
            "JWT required"
            + (f" (roles: {', '.join(policy.jwt.required_roles)})" if policy.jwt.required_roles else "")
            if policy.jwt.required
            else "subscription key only",
        ]
        lines.append(f"  * Protection: {', '.join(safety)}")
        alerts = policy.alerts
        enabled = [
            name
            for name, on in (
                ("throttling", alerts.throttling),
                ("quota", alerts.quota_exceeded),
                ("backend failures", alerts.backend_failure),
                ("auth failures", alerts.auth_failure),
                ("content safety", alerts.content_safety),
                ("PII failures", alerts.pii_failure),
            )
            if on
        ]
        lines.append(f"  * Alert events: {', '.join(enabled) if alerts.enabled and enabled else 'off'}")
    if intent.foundry.enabled and intent.foundry.project:
        project = intent.foundry.project
        lines.append(
            f"* Foundry connection in {project.account_name}/{project.project_name} "
            f"({intent.foundry.auth_type})"
        )
    rotation = intent.key_rotation
    if not rotation.use_primary_key or rotation.key_rotation_enabled:
        slot = "secondary" if not rotation.use_primary_key else "primary"
        lines.append(
            f"* Key rotation state: {slot} key published"
            + (", regeneration on" if rotation.key_rotation_enabled else "")
        )
    return "\n".join(lines)


def list_access_contracts(ctx: ToolContext, environment: str | None = None) -> str:
    env_filter = ctx.environment_name(environment) if environment else None
    records = ctx.access_contracts(env_filter)
    authorizer = ctx.runtime.authorizer
    operator = ctx.persona == "operator"
    visible = [r for r in records if operator or authorizer.owns(r.intent)]
    if not visible:
        scope = "" if operator else " that you own"
        return f"No access contracts{scope} in the contracts repository" + (
            f" for {env_filter}." if env_filter else "."
        )
    lines = [
        "| Use case | Environment | Product | Models | Tokens/min | Owners |",
        "|---|---|---|---|---|---|",
    ]
    for record in sorted(visible, key=lambda r: str(r.path)):
        intent = record.intent
        service = intent.services[0]
        tpm = service.policy.token_limit.tokens_per_minute if service.policy.token_limit else None
        lines.append(
            f"| {intent.use_case.business_unit}/{intent.use_case.use_case_name} | {intent.environment} "
            f"| {intent.product_id(service.code)} | {', '.join(service.policy.allowed_models) or 'all'} "
            f"| {f'{tpm:,}' if tpm else '-'} | {', '.join(intent.metadata.owners) or '-'} |"
        )
    if not operator and len(visible) < len(records):
        lines.append(
            f"\n{len(records) - len(visible)} other contracts are not shown because you do not own them."
        )
    return "\n".join(lines)


def describe_access_contract(
    ctx: ToolContext, business_unit: str, use_case: str, environment: str | None = None
) -> str:
    env_name = ctx.environment_name(environment)
    record = ctx.find_access_contract(business_unit, use_case, env_name)
    if record is None:
        raise ToolInputError(f"No access contract for {business_unit}/{use_case} in {env_name}")
    env = ctx.environment(env_name)
    text = describe_intent(record.intent, env.key_vault.name if env.key_vault else None)
    return f"{text}\n* Contract file: {record.path}"


def _secret_collisions(ctx: ToolContext, intent: AccessContractIntent, vault: str | None) -> list[str]:
    """Secret names that another contract already writes to the same Key Vault."""
    if vault is None:
        return []
    mine = intent.key_vault_secret_names()
    target = intent_key(intent)
    clashes: set[str] = set()
    for record in ctx.access_contracts():
        other = record.intent
        if intent_key(other) == target:
            continue
        try:
            other_env = ctx.environment(other.environment)
        except ToolInputError:
            continue
        if other_env.key_vault is not None and other_env.key_vault.name.lower() == vault.lower():
            clashes |= mine & other.key_vault_secret_names()
    return sorted(clashes)


def intent_key(intent: AccessContractIntent) -> tuple[str, str, str]:
    use = intent.use_case
    return (use.business_unit.lower(), use.use_case_name.lower(), intent.environment.lower())


def _new_contract(ctx: ToolContext, business_unit: str, use_case: str, env_name: str) -> dict[str, Any]:
    # The environment is part of the secret names: environments may share one Key Vault.
    prefix = f"{business_unit}-{use_case}-{SERVICE_CODE}-{env_name}".upper()
    return {
        "kind": "access-contract",
        "environment": env_name,
        "metadata": {"owners": [ctx.caller]},
        "useCase": {"businessUnit": business_unit, "useCaseName": use_case, "environment": env_name.upper()},
        "services": [
            {
                "code": SERVICE_CODE,
                "apis": list(DEFAULT_APIS),
                "keySecretName": f"{prefix}-KEY",
                "endpointSecretName": f"{prefix}-ENDPOINT",
                "policy": {},
            }
        ],
    }


def _published_api_names(ctx: ToolContext, env_name: str) -> set[str]:
    """APIs published through publish contracts in the repository or in pending drafts."""
    names = {a.name for r in ctx.publish_contracts(env_name) for a in r.intent.assets}
    for draft in ctx.drafts.list():
        if draft.intent.kind == "publish-contract" and draft.intent.environment == env_name:
            names |= {a.name for a in draft.intent.assets}
    return names


def _grant_published_apis(
    data: dict[str, Any],
    business_unit: str,
    use_case: str,
    env_name: str,
    tool_apis: list[str],
    agent_apis: list[str],
    calls_per_minute: int | None,
) -> None:
    """Grant published tools and agents through a MULTI service; the LLM service stays untouched."""
    service = next((s for s in data["services"] if s["code"] == "MULTI"), None)
    if service is None:
        prefix = f"{business_unit}-{use_case}-MULTI-{env_name}".upper()
        service = {
            "code": "MULTI",
            "apis": [],
            "keySecretName": f"{prefix}-KEY",
            "publishAllAssetEndpoints": True,
            "policy": {},
        }
        data["services"].append(service)
    for api in [*tool_apis, *agent_apis]:
        if api not in service["apis"]:
            service["apis"].append(api)
    for field, mine, other in (("toolApis", tool_apis, agent_apis), ("agentApis", agent_apis, tool_apis)):
        kept = [a for a in service.get(field, []) if a not in other]
        service[field] = list(dict.fromkeys([*kept, *mine]))
    policy = service.setdefault("policy", {})
    for field, mine in (("toolLimits", tool_apis), ("agentLimits", agent_apis)):
        if calls_per_minute is not None and mine:
            policy[field] = {**policy.get(field, {}), "calls": calls_per_minute}
        if service[field.replace("Limits", "Apis")] and field not in policy:
            raise ToolInputError("Ask the user for the call limit and set asset_calls_per_minute")


def draft_access_contract(
    ctx: ToolContext,
    business_unit: str,
    use_case: str,
    environment: str | None = None,
    models: list[str] | None = None,
    tokens_per_minute: int | None = None,
    monthly_token_quota: int | None = None,
    owners: list[str] | None = None,
    description: str | None = None,
    pii: str | None = None,
    content_safety: bool | None = None,
    jwt_required: bool | None = None,
    required_roles: list[str] | None = None,
    foundry_subscription_id: str | None = None,
    foundry_resource_group: str | None = None,
    foundry_account: str | None = None,
    foundry_project: str | None = None,
    foundry_auth: str | None = None,
    draft_id: str | None = None,
    tool_apis: list[str] | None = None,
    agent_apis: list[str] | None = None,
    asset_calls_per_minute: int | None = None,
) -> str:
    env_name = ctx.environment_name(environment)
    env = ctx.environment(env_name)
    authorizer = ctx.runtime.authorizer
    authorizer.require(Operation.ACCESS_WRITE)
    existing = ctx.find_access_contract(business_unit, use_case, env_name)
    if existing is not None:
        authorizer.require_owner(existing.intent)
        data = existing.intent.model_dump(mode="json", by_alias=True, exclude_none=True)
    else:
        data = _new_contract(ctx, business_unit, use_case, env_name)
    metadata = data.setdefault("metadata", {})
    if owners:
        metadata["owners"] = list(dict.fromkeys([*metadata.get("owners", []), *owners]))
    if description is not None:
        metadata["description"] = description
    policy = data["services"][0].setdefault("policy", {})
    if models is not None:
        policy["allowedModels"] = models
    if tokens_per_minute is not None or monthly_token_quota is not None:
        limit = policy.setdefault("tokenLimit", {})
        if tokens_per_minute is not None:
            limit["tokensPerMinute"] = tokens_per_minute
        if monthly_token_quota is not None:
            limit["tokenQuota"] = monthly_token_quota
            limit["tokenQuotaPeriod"] = "Monthly"
    if pii is not None:
        policy.setdefault("pii", {})["mode"] = pii
    if content_safety is not None:
        policy.setdefault("contentSafety", {})["enabled"] = content_safety
    if jwt_required is not None:
        policy.setdefault("jwt", {})["required"] = jwt_required
    if required_roles is not None:
        policy.setdefault("jwt", {})["requiredRoles"] = required_roles
    coordinates = (foundry_subscription_id, foundry_resource_group, foundry_account, foundry_project)
    if any(coordinates):
        if not all(coordinates):
            raise ToolInputError(
                "A Foundry connection needs the subscription ID, resource group, account and project"
            )
        foundry = data.setdefault("foundry", {})
        foundry["enabled"] = True
        foundry["project"] = {
            "subscriptionId": foundry_subscription_id,
            "resourceGroupName": foundry_resource_group,
            "accountName": foundry_account,
            "projectName": foundry_project,
        }
        if foundry_auth:
            foundry["authType"] = foundry_auth
    unknown_apis: list[str] = []
    if tool_apis or agent_apis:
        tool_apis, agent_apis = list(dict.fromkeys(tool_apis or [])), list(dict.fromkeys(agent_apis or []))
        if set(tool_apis) & set(agent_apis):
            raise ToolInputError("An API is either a tool or an agent, not both")
        _grant_published_apis(
            data, business_unit, use_case, env_name, tool_apis, agent_apis, asset_calls_per_minute
        )
        known = _published_api_names(ctx, env_name)
        unknown_apis = [a for a in [*tool_apis, *agent_apis] if a not in known]
    if existing is None and "tokenLimit" not in policy:
        raise ToolInputError(
            "Ask the user for the expected volume and set tokens_per_minute for a new contract"
        )
    if existing is None and not policy.get("allowedModels"):
        raise ToolInputError("Ask the user which models the use case needs and set models for a new contract")
    try:
        intent = parse_intent(data)
    except ValidationError as exc:
        raise ToolInputError(f"The contract is not valid: {_validation_message(exc)}") from exc
    problems = authorizer.check_caps(intent)
    if problems:
        raise ToolInputError("The admin policy does not allow this: " + "; ".join(problems))
    clashes = _secret_collisions(ctx, intent, env.key_vault.name if env.key_vault else None)
    if clashes:
        raise ToolInputError(
            "Another contract already stores secrets with these names in the same Key Vault: "
            f"{', '.join(clashes)}. Deploying would overwrite its key."
        )
    draft = ctx.drafts.save(
        intent, operation=Operation.ACCESS_WRITE, created_by=ctx.caller, draft_id=draft_id
    )
    action = "Change to the existing contract" if existing is not None else "New contract"
    note = (
        f"Warning: {', '.join(unknown_apis)} not found in publish contracts of {env_name}; "
        "they must already be published on the gateway, otherwise deploying fails.\n\n"
        if unknown_apis
        else ""
    )
    return (
        f"{action} drafted (draft_id: {draft.id}).\n\n"
        f"{describe_intent(intent, env.key_vault.name if env.key_vault else None)}\n\n{note}"
        "Nothing is deployed yet. Confirm the details with the user, then call prepare_change "
        "with this draft_id."
    )


def register_access_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    def list_access_contracts_tool(environment: EnvironmentName = None) -> str:
        return list_access_contracts(ctx, environment)

    def describe_access_contract_tool(
        business_unit: BusinessUnit, use_case: UseCase, environment: EnvironmentName = None
    ) -> str:
        return describe_access_contract(ctx, business_unit, use_case, environment)

    def draft_access_contract_tool(
        business_unit: BusinessUnit,
        use_case: UseCase,
        environment: EnvironmentName = None,
        models: OptionalModels = None,
        tokens_per_minute: Annotated[
            int | None, Field(gt=0, description="Tokens per minute for the use case.")
        ] = None,
        monthly_token_quota: Annotated[
            int | None, Field(gt=0, description="Optional monthly token quota.")
        ] = None,
        owners: Annotated[list[str] | None, Field(description="Extra owners (user principal names).")] = None,
        description: Annotated[
            str | None, Field(max_length=300, description="What the use case does.")
        ] = None,
        pii: PiiMode = None,
        content_safety: Annotated[bool | None, Field(description="Turn content safety on or off.")] = None,
        jwt_required: Annotated[bool | None, Field(description="Also require an Entra ID token.")] = None,
        required_roles: Annotated[
            list[str] | None, Field(description="App roles the token must have.")
        ] = None,
        foundry_subscription_id: Annotated[
            str | None, Field(description="Foundry project subscription ID.")
        ] = None,
        foundry_resource_group: Annotated[
            str | None, Field(description="Foundry project resource group.")
        ] = None,
        foundry_account: Annotated[str | None, Field(description="Foundry account (resource) name.")] = None,
        foundry_project: Annotated[str | None, Field(description="Foundry project name.")] = None,
        foundry_auth: Annotated[
            Literal["ProjectManagedIdentity", "ApiKey"] | None,
            Field(description="How Foundry authenticates."),
        ] = None,
        draft_id: Annotated[
            str | None, Field(description="Revise this draft instead of creating a new one.")
        ] = None,
        tool_apis: Annotated[
            list[str] | None, Field(description="Published MCP tool API names to grant.")
        ] = None,
        agent_apis: Annotated[
            list[str] | None, Field(description="Published A2A agent API names to grant.")
        ] = None,
        asset_calls_per_minute: Annotated[
            int | None, Field(gt=0, description="Calls per minute for the granted tools or agents.")
        ] = None,
    ) -> str:
        return draft_access_contract(
            ctx,
            business_unit,
            use_case,
            environment,
            models,
            tokens_per_minute,
            monthly_token_quota,
            owners,
            description,
            pii,
            content_safety,
            jwt_required,
            required_roles,
            foundry_subscription_id,
            foundry_resource_group,
            foundry_account,
            foundry_project,
            foundry_auth,
            draft_id,
            tool_apis,
            agent_apis,
            asset_calls_per_minute,
        )

    registry.add(
        ToolSpec(
            "list_access_contracts",
            list_access_contracts_tool,
            "Inventory of access contracts (use cases) with models and limits; consumers see their own.",
        )
    )
    registry.add(
        ToolSpec(
            "describe_access_contract",
            describe_access_contract_tool,
            "Plain-language description of one use case's access contract (no secret values).",
        )
    )
    registry.add(
        ToolSpec(
            "draft_access_contract",
            draft_access_contract_tool,
            "Create or update an access contract draft (models, limits, PII, safety, JWT, Foundry). "
            "Unset fields keep their current values. Changes nothing until prepared and approved.",
        )
    )
