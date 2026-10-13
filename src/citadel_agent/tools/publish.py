# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Publish contract tool (platform operators): publish MCP tools and A2A agents through the gateway.

One call adds or updates one asset in a publish contract (``contracts/publish/<name>/<env>``); call
it again with the same ``contract_name`` (or ``draft_id``) to add more assets to the same contract.
Publishing does not grant access: use ``draft_access_contract`` with ``tool_apis``/``agent_apis``
afterwards. (No postponed annotations.)
"""

import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError

from citadel_agent.changes.drafts import DraftError
from citadel_agent.contracts.access import KNOWN_LLM_APIS
from citadel_agent.contracts.publish import PublishContractIntent
from citadel_agent.policy.admin import Operation
from citadel_agent.safety.secret_ingress import find_secret_literals
from citadel_agent.tools.access import _validation_message
from citadel_agent.tools.backend import IDENTITY_API_VERSION, OPERATOR_ONLY, ROLE_ASSIGNMENTS_API_VERSION
from citadel_agent.tools.context import ToolContext
from citadel_agent.tools.registry import ToolInputError, ToolRegistry, ToolSpec

PublishAssetType = Literal["mcp-from-api", "mcp-existing", "a2a", "foundry-agent"]
FOUNDRY_RESOURCE = "https://ai.azure.com"
AGENT_CONSUMER_ROLES = frozenset({"Foundry Agent Consumer", "Azure AI User"})
PUBLISH_TRACK_FALLBACK = "publish-contract-version 1.1.0-preview"

_PROJECT_ID = re.compile(
    r"^/subscriptions/(?P<subscription>[0-9a-f-]{36})/resourceGroups/(?P<group>[^/]+)"
    r"/providers/Microsoft\.CognitiveServices/accounts/(?P<account>[^/]+)/projects/(?P<project>[^/]+)/?$",
    re.IGNORECASE,
)
_KEY_VAULT_SECRET_URI = re.compile(
    r"^https://[a-z0-9-]{3,24}\.vault\.[a-z0-9.-]+/secrets/[A-Za-z0-9-]{1,127}(/[0-9a-f]{32})?$",
    re.IGNORECASE,
)
COMMON = frozenset({"display_name", "description", "path"})
_BACKEND_AUTH = ("auth_type", "auth_resource", "key_vault_secret_uri", "auth_header_name")
# Parameters each asset type accepts (besides the common ones) and the ones it requires.
ACCEPTED: dict[str, frozenset[str]] = {
    "mcp-from-api": frozenset({"source_api", "operations", "subscription_required"}),
    "mcp-existing": frozenset({"backend_url", "transport", *_BACKEND_AUTH, "subscription_required"}),
    "a2a": frozenset({"backend_url", "agent_id", "agent_card_url", *_BACKEND_AUTH, "subscription_required"}),
    "foundry-agent": frozenset({"foundry_project_id", "foundry_agent", "subscription_required"}),
}
REQUIRED: dict[str, tuple[str, ...]] = {
    "mcp-from-api": ("source_api", "operations"),
    "mcp-existing": ("backend_url",),
    "a2a": ("backend_url", "agent_id", "agent_card_url"),
    "foundry-agent": ("foundry_project_id", "foundry_agent"),
}


def _given(value: Any) -> bool:
    return value is not None and value != [] and value != ""


def _check_inputs(asset_type: str, values: dict[str, Any]) -> None:
    findings = find_secret_literals({key: value for key, value in values.items() if _given(value)})
    if findings:
        where = ", ".join(f.path.removeprefix("$.").split("[")[0] for f in findings)
        raise ToolInputError(
            f"Rejected: {where} looks like a secret. Never share keys or tokens; store the key in Key "
            "Vault and pass its secret URI as key_vault_secret_uri."
        )
    given = {key for key, value in values.items() if _given(value)}
    unused = sorted(given - ACCEPTED[asset_type] - COMMON)
    if unused:
        raise ToolInputError(f"{asset_type} does not use: {', '.join(unused)}")
    missing = [key for key in REQUIRED[asset_type] if key not in given]
    if missing:
        raise ToolInputError(f"{asset_type} needs: {', '.join(missing)}")
    for key in ("backend_url", "agent_card_url"):
        parts = urlsplit(values.get(key) or "")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ToolInputError(f"{key} must not carry a query string or credentials; use Key Vault")


def _backend(name: str, values: dict[str, Any]) -> dict[str, Any]:
    auth_type = values.get("auth_type") or "none"
    backend: dict[str, Any] = {"url": values["backend_url"], "authType": auth_type}
    secret_uri = values.get("key_vault_secret_uri")
    if auth_type in {"api-key-header", "api-key-bearer"}:
        if not secret_uri or not _KEY_VAULT_SECRET_URI.match(secret_uri):
            raise ToolInputError(
                f"{auth_type} needs key_vault_secret_uri, a Key Vault secret URI "
                "(https://<vault>.vault.azure.net/secrets/<name>); never paste the key itself"
            )
        config: dict[str, Any] = {"namedValueKey": f"{name}-key"[:60], "keyVaultSecretUri": secret_uri}
        if values.get("auth_header_name") and auth_type == "api-key-header":
            config["headerName"] = values["auth_header_name"]
        backend["authConfig"] = config
    elif secret_uri or values.get("auth_header_name"):
        raise ToolInputError("key_vault_secret_uri and auth_header_name apply to api-key auth only")
    elif auth_type == "managed-identity":
        resource = values.get("auth_resource") or (FOUNDRY_RESOURCE if values.get("agent_id") else None)
        backend["authConfig"] = {"resource": resource} if resource else {}
    return backend


def _foundry(values: dict[str, Any]) -> dict[str, str]:
    match = _PROJECT_ID.match(values["foundry_project_id"])
    if not match:
        raise ToolInputError(
            "foundry_project_id must be the project's resource ID: /subscriptions/<id>/resourceGroups/<rg>"
            "/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>"
        )
    return {
        **match.groupdict(),
        "id": values["foundry_project_id"].rstrip("/"),
        "agent": values["foundry_agent"],
    }


def _asset(asset_type: str, name: str, values: dict[str, Any]) -> dict[str, Any]:
    asset: dict[str, Any] = {
        "name": name,
        "displayName": values.get("display_name") or name.replace("-", " ").title(),
        "path": values.get("path") or name,
        "description": values.get("description") or "",
    }
    if values.get("subscription_required") is not None:
        asset["subscriptionRequired"] = values["subscription_required"]
    if asset_type == "mcp-from-api":
        asset.update(
            assetType=asset_type, sourceApiName=values["source_api"], operationNames=values["operations"]
        )
    elif asset_type == "mcp-existing":
        asset.update(assetType=asset_type, backend=_backend(name, values))
        if values.get("transport"):
            asset["transportType"] = values["transport"]
    elif asset_type == "a2a":
        asset.update(
            assetType="a2a",
            agentId=values["agent_id"],
            agentCardBackendUrl=values["agent_card_url"],
            backend=_backend(name, values),
        )
    else:
        project = _foundry(values)
        base = (
            f"https://{project['account']}.services.ai.azure.com/api/projects/{project['project']}"
            f"/agents/{project['agent']}/endpoint/protocols/a2a"
        )
        asset.update(
            assetType="a2a",
            agentId=project["agent"],
            agentCardBackendUrl=f"{base}/agentCard/v1.0",
            backend={
                "url": base,
                "authType": "managed-identity",
                "authConfig": {"resource": FOUNDRY_RESOURCE},
            },
        )
        asset.setdefault("subscriptionRequired", True)
    return asset


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower())[:limit].strip("-")


def _gateway_identity(ctx: ToolContext, env: Any) -> tuple[Any, str]:
    """The gateway's managed identity and its principal ID (a placeholder when it cannot be read)."""
    identity = env.apim_managed_identity
    placeholder = "<principal-id-of-the-gateway-managed-identity>"
    if identity is None:
        return None, placeholder
    identity_id = (
        f"/subscriptions/{identity.subscription_id}/resourceGroups/{identity.resource_group_name}"
        f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{identity.name}"
    )
    try:
        return identity, ctx.runtime.arm.get(identity_id, IDENTITY_API_VERSION)["properties"]["principalId"]
    except Exception:  # the lookup only improves the command; the manual step is emitted regardless
        return identity, placeholder


def _has_consumer_role(ctx: ToolContext, scope: str, principal: str) -> bool:
    arm = ctx.runtime.arm
    try:
        assignments = arm.list(
            f"{scope}/providers/Microsoft.Authorization/roleAssignments",
            ROLE_ASSIGNMENTS_API_VERSION,
            {"$filter": f"assignedTo('{principal}')"},
        )
        for assignment in assignments:
            definition = arm.get(assignment["properties"]["roleDefinitionId"], ROLE_ASSIGNMENTS_API_VERSION)
            if definition["properties"]["roleName"] in AGENT_CONSUMER_ROLES:
                return True
    except Exception:  # unreadable role assignments are never assumed to be granted
        return False
    return False


def _foundry_prerequisites(ctx: ToolContext, env: Any, project: dict[str, str]) -> list[dict[str, Any]]:
    """Manual steps for a Foundry agent published as A2A; the agent never performs them itself."""
    identity, principal = _gateway_identity(ctx, env)
    host = f"https://{project['account']}.services.ai.azure.com/api/projects/{project['project']}"
    agent_url = f"{host}/agents/{project['agent']}"
    steps: list[dict[str, Any]] = []
    if not (identity and _has_consumer_role(ctx, project["id"], principal)):
        who = identity.name if identity else "the gateway managed identity"
        steps.append(
            {
                "stepId": f"foundry-rbac-{_slug(project['project'], 28)}",
                "description": (
                    f"Grant {who} the Foundry Agent Consumer role (or Azure AI User) on Foundry project "
                    f"{project['project']}"
                ),
                "commands": [
                    f"az role assignment create --assignee-object-id {principal} --assignee-principal-type "
                    f'ServicePrincipal --role "Foundry Agent Consumer" --scope {project["id"]}'
                ],
                "verification": (
                    f"az role assignment list --assignee {principal} --scope {project['id']} -o table"
                ),
            }
        )
    steps.append(
        {
            "stepId": f"foundry-a2a-{_slug(project['agent'], 28)}",
            "description": (
                f"Enable incoming A2A on Foundry agent {project['agent']}: PATCH the agent with an "
                "agent_card and protocol_configuration { responses: {}, a2a: {} } (see 'Enable incoming A2A "
                "on a Foundry agent' in the Foundry docs and the publish contract guide, section 8)"
            ),
            "commands": [
                f'az rest --method patch --resource {FOUNDRY_RESOURCE} --url "{agent_url}?api-version=v1" '
                "--body @agent-a2a.json"
            ],
            "verification": (
                f"az rest --method get --resource {FOUNDRY_RESOURCE} "
                f'--url "{agent_url}/endpoint/protocols/a2a/agentCard/v1.0"'
            ),
        }
    )
    return steps


def _collisions(ctx: ToolContext, intent: PublishContractIntent, own_draft: str | None) -> list[str]:
    """Names and gateway paths already used by other publish contracts or pending drafts."""
    others: list[tuple[str, PublishContractIntent]] = [
        (f"contract {r.intent.name}", r.intent) for r in ctx.publish_contracts(intent.environment)
    ]
    others += [
        (f"draft {d.id}", d.intent)
        for d in ctx.drafts.list()
        if d.id != own_draft and isinstance(d.intent, PublishContractIntent)
    ]
    problems: list[str] = []
    for asset in intent.assets:
        path = asset.gateway_path(intent.use_asset_type_path_prefix)
        if asset.name in KNOWN_LLM_APIS:
            problems.append(f"{asset.name} is the name of a built-in LLM API")
        for where, other in others:
            if other.environment != intent.environment or other.name == intent.name:
                continue
            for existing in other.assets:
                if existing.name == asset.name:
                    problems.append(f"the name {asset.name} is already published by {where}")
                if existing.gateway_path(other.use_asset_type_path_prefix) == path:
                    problems.append(f"the gateway path /{path} is already used by {where} ({existing.name})")
    return list(dict.fromkeys(problems))


def _load_base(
    ctx: ToolContext, env_name: str, contract_name: str | None, draft_id: str | None
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str | None]:
    """The contract to extend and the manual steps already recorded for it."""
    if draft_id:
        try:
            draft = ctx.drafts.load(draft_id)
        except DraftError as exc:
            raise ToolInputError(str(exc)) from exc
        intent = draft.intent
        if not isinstance(intent, PublishContractIntent) or intent.environment != env_name:
            raise ToolInputError(f"Draft {draft_id} is not a publish contract draft for {env_name}")
        if contract_name and contract_name != intent.name:
            raise ToolInputError(f"Draft {draft_id} is for contract {intent.name}, not {contract_name}")
        steps = {p["stepId"]: dict(p) for p in draft.prerequisites}
        return intent.model_dump(mode="json", by_alias=True, exclude_none=True), steps, draft_id
    pending = [
        d
        for d in ctx.drafts.list()
        if isinstance(d.intent, PublishContractIntent)
        and d.intent.environment == env_name
        and d.intent.name == contract_name
    ]
    if pending:
        raise ToolInputError(
            f"Contract {contract_name} already has draft {pending[0].id}; pass it as draft_id to add to it"
        )
    existing = next(
        (r.intent for r in ctx.publish_contracts(env_name) if r.intent.name == contract_name), None
    )
    if existing is not None:
        return existing.model_dump(mode="json", by_alias=True, exclude_none=True), {}, None
    data = {
        "kind": "publish-contract",
        "environment": env_name,
        "name": contract_name,
        "metadata": {"owners": [ctx.caller]},
        "assets": [],
    }
    return data, {}, None


def _preview_note(ctx: ToolContext) -> str:
    try:
        track = f"publish-contract-version {ctx.runtime.lock.upstream.release['publish-contract-version']}"
    except (AttributeError, KeyError, TypeError):
        track = PUBLISH_TRACK_FALLBACK
    return (
        f"Publish contracts are a preview feature ({track}); the agent only writes them when the "
        "gateway runs exactly that version. Try them in dev or test first."
    )


def _describe(asset: Any, gateway_url: str, use_prefix: bool) -> str:
    url = f"{gateway_url.rstrip('/')}/{asset.gateway_path(use_prefix)}"
    kind = "A2A agent" if asset.asset_type == "a2a" else "MCP tool"
    backend = asset.backend.auth_type if asset.backend else "source API backend"
    keyed = asset.subscription_required
    if keyed is None:
        keyed = asset.asset_type != "a2a"
    return (
        f"{kind} `{asset.name}` ({asset.asset_type}) at {url}; backend auth: {backend}; "
        f"{'APIM subscription key required' if keyed else 'anonymous at the gateway'}"
    )


def draft_publish_contract(
    ctx: ToolContext,
    asset_type: str,
    name: str,
    environment: str | None = None,
    contract_name: str | None = None,
    display_name: str | None = None,
    description: str | None = None,
    path: str | None = None,
    source_api: str | None = None,
    operations: list[str] | None = None,
    backend_url: str | None = None,
    transport: str | None = None,
    auth_type: str | None = None,
    auth_resource: str | None = None,
    key_vault_secret_uri: str | None = None,
    auth_header_name: str | None = None,
    agent_id: str | None = None,
    agent_card_url: str | None = None,
    foundry_project_id: str | None = None,
    foundry_agent: str | None = None,
    subscription_required: bool | None = None,
    draft_id: str | None = None,
) -> str:
    if ctx.persona != "operator":
        raise PermissionError("Publishing tools and agents needs the platform team; ask them to publish it.")
    env_name = ctx.environment_name(environment)
    env = ctx.environment(env_name)
    ctx.runtime.authorizer.require(Operation.PUBLISH_WRITE)
    if asset_type not in ACCEPTED:
        raise ToolInputError(f"asset_type must be one of: {', '.join(ACCEPTED)}")
    values = {
        key: value
        for key, value in {
            "display_name": display_name,
            "description": description,
            "path": path,
            "source_api": source_api,
            "operations": operations,
            "backend_url": backend_url,
            "transport": transport,
            "auth_type": auth_type,
            "auth_resource": auth_resource,
            "key_vault_secret_uri": key_vault_secret_uri,
            "auth_header_name": auth_header_name,
            "agent_id": agent_id,
            "agent_card_url": agent_card_url,
            "foundry_project_id": foundry_project_id,
            "foundry_agent": foundry_agent,
            "subscription_required": subscription_required,
        }.items()
        if _given(value)
    }
    _check_inputs(asset_type, values)
    base, steps, own_draft = _load_base(
        ctx, env_name, contract_name or (None if draft_id else name), draft_id
    )
    contract = base["name"]
    asset = _asset(asset_type, name, values)
    assets = [a for a in base["assets"] if a["name"] != name]
    replaced = len(assets) != len(base["assets"])
    base["assets"] = [*assets, asset]
    try:
        intent = PublishContractIntent.model_validate(base)
    except ValidationError as exc:
        raise ToolInputError(f"The publish contract is not valid: {_validation_message(exc)}") from exc
    problems = _collisions(ctx, intent, own_draft)
    if problems:
        raise ToolInputError("Publishing would collide with existing assets: " + "; ".join(problems))
    if asset_type == "foundry-agent":
        steps.update({s["stepId"]: s for s in _foundry_prerequisites(ctx, env, _foundry(values))})
    prerequisites = list(steps.values())
    draft = ctx.drafts.save(
        intent,
        operation=Operation.PUBLISH_WRITE,
        created_by=ctx.caller,
        step_id=f"publish-{_slug(contract, 32)}",
        draft_id=own_draft,
        prerequisites=tuple(prerequisites),
    )
    published = next(a for a in intent.assets if a.name == name)
    lines = [
        f"Publish contract {contract} drafted for {env_name} (draft_id: {draft.id}).",
        f"* {'Updated' if replaced else 'Added'} "
        + _describe(published, env.gateway_url, intent.use_asset_type_path_prefix),
        f"* Assets in this contract: {', '.join(a.name for a in intent.assets)}",
    ]
    lines += [f"* Prerequisite (manual): {p['description']}" for p in prerequisites]
    if asset_type == "mcp-from-api":
        lines.append(
            "Warning: the source API must not require its own subscription key, otherwise tool calls fail "
            "with 401 (initialize and tools/list still work)."
        )
    if published.asset_type == "a2a":
        lines.append(
            "Warning: A2A support is in preview (JSON-RPC only, text only, no streaming) and the gateway "
            "authenticates to the agent with its managed identity."
        )
    lines.append(f"Warning: {_preview_note(ctx)}")
    lines.append(
        "Nothing is deployed yet and nobody has access to it. To grant a use case access, call "
        f"draft_access_contract with tool_apis or agent_apis [{name}], then call prepare_change with this "
        "draft_id and the access draft_ids (the publish step runs first)."
    )
    return "\n".join(lines)


def register_publish_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    def draft_publish_contract_tool(
        asset_type: Annotated[
            PublishAssetType,
            Field(description="mcp-from-api, mcp-existing (remote MCP), a2a, or foundry-agent."),
        ],
        name: Annotated[str, Field(description="API name: lowercase letters, digits, hyphens.")],
        environment: Annotated[
            str | None, Field(description="Environment; defaults to the current one.")
        ] = None,
        contract_name: Annotated[
            str | None, Field(description="Publish contract to add to (default: the asset name).")
        ] = None,
        display_name: Annotated[str | None, Field(description="Human-readable name.")] = None,
        description: Annotated[str | None, Field(max_length=300, description="What it does.")] = None,
        path: Annotated[str | None, Field(description="Gateway path (default: the name).")] = None,
        source_api: Annotated[
            str | None, Field(description="mcp-from-api: existing gateway API to expose as MCP.")
        ] = None,
        operations: Annotated[
            list[str] | None, Field(description="mcp-from-api: operation names to expose as tools.")
        ] = None,
        backend_url: Annotated[
            str | None, Field(description="mcp-existing or a2a: HTTPS URL of the server or agent.")
        ] = None,
        transport: Annotated[
            Literal["streamable", "sse"] | None, Field(description="mcp-existing transport.")
        ] = None,
        auth_type: Annotated[
            Literal["none", "managed-identity", "api-key-header", "api-key-bearer"] | None,
            Field(description="How the gateway authenticates to the backend (default none)."),
        ] = None,
        auth_resource: Annotated[
            str | None, Field(description="managed-identity audience (a2a default https://ai.azure.com).")
        ] = None,
        key_vault_secret_uri: Annotated[
            str | None, Field(description="api-key auth: Key Vault secret URI. Never the key itself.")
        ] = None,
        auth_header_name: Annotated[
            str | None, Field(description="api-key-header: header that carries the key.")
        ] = None,
        agent_id: Annotated[str | None, Field(description="a2a: agent ID.")] = None,
        agent_card_url: Annotated[str | None, Field(description="a2a: HTTPS URL of the agent card.")] = None,
        foundry_project_id: Annotated[
            str | None, Field(description="foundry-agent: Foundry project resource ID.")
        ] = None,
        foundry_agent: Annotated[str | None, Field(description="foundry-agent: agent name.")] = None,
        subscription_required: Annotated[
            bool | None, Field(description="Require an APIM subscription key from callers.")
        ] = None,
        draft_id: Annotated[str | None, Field(description="Add to or revise this publish draft.")] = None,
    ) -> str:
        return draft_publish_contract(
            ctx,
            asset_type,
            name,
            environment=environment,
            contract_name=contract_name,
            display_name=display_name,
            description=description,
            path=path,
            source_api=source_api,
            operations=operations,
            backend_url=backend_url,
            transport=transport,
            auth_type=auth_type,
            auth_resource=auth_resource,
            key_vault_secret_uri=key_vault_secret_uri,
            auth_header_name=auth_header_name,
            agent_id=agent_id,
            agent_card_url=agent_card_url,
            foundry_project_id=foundry_project_id,
            foundry_agent=foundry_agent,
            subscription_required=subscription_required,
            draft_id=draft_id,
        )

    registry.add(
        ToolSpec(
            "draft_publish_contract",
            draft_publish_contract_tool,
            "Draft publishing one MCP tool or A2A agent (including a Foundry agent) through the gateway "
            "(preview). Repeat with the same contract_name or draft_id to add assets. Grants no access. "
            "Changes nothing until prepared and approved.",
            personas=OPERATOR_ONLY,
        )
    )
