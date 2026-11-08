# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Strict, preview-first customer installation of the private hosted topology.

No Azure mutation occurs unless the caller explicitly supplies ``--apply``.
Customer-owned app registrations and secret values are never created or replaced.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator, model_validator

from citadel_agent.contracts.common import AzureResourceRef, CitadelModel, GuidStr
from citadel_agent.portal.product_contracts import PortalHubConfig
from citadel_agent.proc import CommandError, run_command
from citadel_agent.safety.secret_ingress import find_secret_literals

logger = logging.getLogger(__name__)
Command = Callable[[list[str]], Any]
_PREFIX = r"^[a-z][a-z0-9-]{1,14}[a-z0-9]$"
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9./_-]+@sha256:[0-9a-f]{64}$")
_API_VERSION = {
    "Microsoft.ManagedIdentity/userAssignedIdentities": "2024-11-30",
    "Microsoft.ApiManagement/service": "2024-05-01",
    "Microsoft.DocumentDB/databaseAccounts": "2025-04-15",
    "Microsoft.KeyVault/vaults": "2024-11-01",
    "Microsoft.CognitiveServices/accounts": "2025-06-01",
    "Microsoft.OperationalInsights/workspaces": "2025-02-01",
}


class EntraInstallConfig(CitadelModel):
    client_id: GuidStr
    api_client_id: GuidStr
    delegated_scope_name: str = Field(default="access_as_user", pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,99}$")
    client_secret_name: str = Field(pattern=r"^[A-Za-z0-9-]{1,127}$")


class HostingNetworkConfig(CitadelModel):
    address_prefix: ipaddress.IPv4Network = ipaddress.IPv4Network("10.244.0.0/20")
    agent_prefix: ipaddress.IPv4Network = ipaddress.IPv4Network("10.244.0.0/23")
    bff_prefix: ipaddress.IPv4Network = ipaddress.IPv4Network("10.244.2.0/26")
    endpoint_prefix: ipaddress.IPv4Network = ipaddress.IPv4Network("10.244.2.64/27")

    @model_validator(mode="after")
    def _dedicated_subnets(self) -> HostingNetworkConfig:
        subnets = (self.agent_prefix, self.bff_prefix, self.endpoint_prefix)
        private_ranges = (
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"),
        )
        if not any(self.address_prefix.subnet_of(network) for network in private_ranges) or any(
            not subnet.subnet_of(self.address_prefix) for subnet in subnets
        ):
            raise ValueError("all dedicated subnets must be within a private portal IPv4 address space")
        if any(a.overlaps(b) for i, a in enumerate(subnets) for b in subnets[i + 1 :]):
            raise ValueError("agent, BFF and private-endpoint subnets must not overlap")
        if (
            self.agent_prefix.prefixlen > 23
            or self.bff_prefix.prefixlen > 26
            or self.endpoint_prefix.prefixlen > 27
        ):
            raise ValueError("subnets must be at least /23 for agent, /26 for BFF and /27 for endpoints")
        return self


class HostingInstallConfig(CitadelModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    subscription_id: GuidStr
    resource_group_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._()-]{0,88}[A-Za-z0-9_-]$")
    name_prefix: str = Field(pattern=_PREFIX)
    web_app: AzureResourceRef
    bff_identity: AzureResourceRef
    key_vault: AzureResourceRef
    usage_account: AzureResourceRef
    model_account: AzureResourceRef
    logs: AzureResourceRef
    entra: EntraInstallConfig
    hub: PortalHubConfig
    network: HostingNetworkConfig = Field(default_factory=HostingNetworkConfig)
    is_zone_redundant: bool = True
    cosmos_max_throughput: int = Field(default=1000, ge=1000, le=100_000, multiple_of=1000)
    should_create_gateway_private_endpoint: bool = True
    portal_origin: str | None = None

    @field_validator("name_prefix")
    @classmethod
    def _no_consecutive_hyphens(cls, value: str) -> str:
        if "--" in value:
            raise ValueError("the resource prefix cannot contain consecutive hyphens")
        return value

    @field_validator("web_app", "bff_identity", "key_vault", "usage_account", "model_account", "logs")
    @classmethod
    def _safe_resource_names(cls, value: AzureResourceRef) -> AzureResourceRef:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value.name) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._()-]{0,89}", value.resource_group_name
        ):
            raise ValueError("existing resource references must contain names, not ARM paths or URLs")
        return value

    @field_validator("portal_origin")
    @classmethod
    def _origin(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
            ):
                raise ValueError("portal origin must be a canonical HTTPS origin")
        return value

    @model_validator(mode="after")
    def _same_subscription(self) -> HostingInstallConfig:
        if find_secret_literals(self.model_dump(mode="json", by_alias=True)):
            raise ValueError(
                "installation configuration must contain resource and secret references, not credentials"
            )
        for ref in (
            self.web_app,
            self.bff_identity,
            self.key_vault,
            self.usage_account,
            self.model_account,
            self.logs,
        ):
            if ref.subscription_id.lower() != self.subscription_id.lower():
                raise ValueError("this installer requires all resource references in the target subscription")
        if self.hub.scope.subscription_id.lower() != self.subscription_id.lower():
            raise ValueError("the hub must be in the installation subscription")
        if not self.hub.initial_grants:
            raise ValueError("at least one administrator-owned initial hub grant is required")
        if not self.hub.github_token_secret_name:
            raise ValueError("a Key Vault reference for the canonical GitHub credential is required")
        if self.entra.client_secret_name.lower() == self.hub.github_token_secret_name.lower():
            raise ValueError("BFF and canonical repository credentials must use separate secrets")
        if (
            self.bff_identity.resource_group_name.lower() == self.resource_group_name.lower()
            and self.bff_identity.name.lower() == f"id-{self.name_prefix}-agent"
        ):
            raise ValueError("the existing BFF identity must be distinct from the generated agent identity")
        return self

    def resource_id(self, ref: AzureResourceRef, resource_type: str) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{ref.resource_group_name}"
            f"/providers/{resource_type}/{ref.name}"
        )

    @property
    def apim(self) -> AzureResourceRef:
        return AzureResourceRef(
            subscription_id=self.subscription_id,
            resource_group_name=self.hub.apim_resource_group,
            name=self.hub.apim_name,
        )


def az_json(arguments: list[str]) -> Any:
    result = run_command("az", [*arguments, "--output", "json"], timeout=1800)
    return json.loads(result.stdout) if result.stdout.strip() else None


def _read(config: HostingInstallConfig, ref: AzureResourceRef, kind: str, command: Command) -> dict[str, Any]:
    return command(
        [
            "resource",
            "show",
            "--ids",
            config.resource_id(ref, kind),
            "--api-version",
            _API_VERSION[kind],
            "--subscription",
            config.subscription_id,
        ]
    )


def _region(value: str) -> str:
    return value.replace(" ", "").lower()


def preflight(config: HostingInstallConfig, command: Command = az_json) -> dict[str, Any]:
    """Resolve and verify all deployment-owned targets before requesting a resource change."""
    account = command(["account", "show", "--subscription", config.subscription_id])
    if account["id"].lower() != config.subscription_id.lower() or (
        account["tenantId"].lower() != config.hub.scope.tenant_id.lower()
    ):
        raise ValueError("selected subscription and hub tenant do not match Azure authentication")
    web = command(
        [
            "webapp",
            "show",
            "--subscription",
            config.subscription_id,
            "--resource-group",
            config.web_app.resource_group_name,
            "--name",
            config.web_app.name,
        ]
    )
    if web.get("reserved") is not True or "linux" not in web.get("kind", "").lower():
        raise ValueError("the target must be an existing dedicated Linux App Service, not a Windows app")
    plan = command(
        ["appservice", "plan", "show", "--ids", web["serverFarmId"], "--subscription", config.subscription_id]
    )
    if plan.get("sku", {}).get("tier", "").lower() not in {
        "basic",
        "standard",
        "premium",
        "premiumv2",
        "premiumv3",
        "premiumv4",
        "isolated",
        "isolatedv2",
    }:
        raise ValueError("the App Service plan must support VNet integration and Always On (Basic or higher)")
    apim = _read(config, config.apim, "Microsoft.ApiManagement/service", command)
    location = _region(apim["location"])
    if location != _region(web["location"]):
        raise ValueError("the existing App Service must be in the same region as the selected APIM")
    if config.is_zone_redundant:
        locations = command(["account", "list-locations", "--subscription", config.subscription_id])
        selected = next((item for item in locations if _region(item["name"]) == location), {})
        if not selected.get("availabilityZoneMappings"):
            raise ValueError(
                "the selected subscription region does not advertise availability zones; "
                "review regional support or explicitly choose isZoneRedundant=false"
            )
    origin = config.portal_origin or f"https://{web['defaultHostName']}"
    if urlsplit(origin).hostname not in {
        host.lower() for host in web.get("hostNames", [web["defaultHostName"]])
    }:
        raise ValueError("the portal origin must be an HTTPS host bound to the selected App Service")
    if config.hub.gateway_url.rstrip("/") != apim["properties"].get("gatewayUrl", "").rstrip("/"):
        raise ValueError("hub.gatewayUrl must match the selected APIM gateway URL")
    if not config.should_create_gateway_private_endpoint and (
        apim["properties"].get("publicNetworkAccess") == "Disabled"
        or apim["properties"].get("virtualNetworkType") == "Internal"
    ):
        raise ValueError(
            "a private gateway requires the gateway private endpoint; do not relax its network access"
        )
    if config.should_create_gateway_private_endpoint:
        result = command(
            [
                "rest",
                "--method",
                "get",
                "--url",
                f"https://management.azure.com{apim['id']}/privateLinkResources?api-version=2024-05-01",
            ]
        )
        if not any(
            item.get("properties", {}).get("groupId") == "Gateway" for item in result.get("value", [])
        ):
            raise ValueError("this APIM does not support a Gateway private endpoint")
    identity = _read(config, config.bff_identity, "Microsoft.ManagedIdentity/userAssignedIdentities", command)
    vault = _read(config, config.key_vault, "Microsoft.KeyVault/vaults", command)
    if vault["properties"].get("enableRbacAuthorization") is not True or (
        vault["properties"].get("enablePurgeProtection") is not True
    ):
        raise ValueError(
            "the existing Key Vault must use Azure RBAC and purge protection; it will not be modified"
        )
    usage = _read(config, config.usage_account, "Microsoft.DocumentDB/databaseAccounts", command)
    if _https_host(config.hub.usage_endpoint) != _https_host(usage["properties"]["documentEndpoint"]):
        raise ValueError("the configured usage endpoint does not match the existing Cosmos account")
    database_id = usage["id"] + "/sqlDatabases/" + config.hub.usage_database
    command(["resource", "show", "--ids", database_id, "--api-version", "2025-04-15"])
    model = _read(config, config.model_account, "Microsoft.CognitiveServices/accounts", command)
    subdomain = model["properties"].get("customSubDomainName")
    if not subdomain or _https_host(config.hub.model_endpoint) not in {
        f"{subdomain}.openai.azure.com",
        f"{subdomain}.cognitiveservices.azure.com",
        f"{subdomain}.services.ai.azure.com",
    }:
        raise ValueError(
            "the model endpoint must match the selected account's Azure OpenAI-compatible custom domain"
        )
    command(
        [
            "resource",
            "show",
            "--ids",
            model["id"] + "/deployments/" + config.hub.model_deployment,
            "--api-version",
            "2025-06-01",
        ]
    )
    logs = _read(config, config.logs, "Microsoft.OperationalInsights/workspaces", command)
    if logs["properties"].get("publicNetworkAccessForQuery") == "Disabled":
        raise ValueError(
            "the gateway log workspace requires an Azure Monitor private-link topology extension; "
            "its query network settings will not be relaxed"
        )
    if (config.hub.log_analytics_workspace_id or "").lower() != logs["properties"]["customerId"].lower():
        raise ValueError("the operational workspace ID must match the existing gateway log workspace")
    _verify_entra(config, origin, identity["properties"], command)
    expected_vnet = (
        f"/subscriptions/{config.subscription_id}/resourceGroups/{config.resource_group_name}"
        f"/providers/Microsoft.Network/virtualNetworks/vnet-{config.name_prefix}"
    )
    existing_subnet = web.get("virtualNetworkSubnetId")
    if existing_subnet and existing_subnet.lower() != (expected_vnet + "/subnets/bff").lower():
        raise ValueError(
            "the existing App Service is integrated with another VNet; no network will be replaced"
        )
    vnets = command(["network", "vnet", "list", "--subscription", config.subscription_id])
    for vnet in vnets:
        if vnet["id"].lower() == expected_vnet.lower():
            if (vnet.get("tags") or {}).get("managedBy") != "citadel-installer":
                raise ValueError("the target VNet already exists but is not owned by this installer")
            expected_prefixes = {
                "agent": str(config.network.agent_prefix),
                "bff": str(config.network.bff_prefix),
                "endpoints": str(config.network.endpoint_prefix),
            }
            actual_prefixes = {item["name"]: item.get("addressPrefix") for item in vnet.get("subnets", [])}
            if actual_prefixes != expected_prefixes or vnet.get("addressSpace", {}).get(
                "addressPrefixes"
            ) != [str(config.network.address_prefix)]:
                raise ValueError(
                    "the existing portal network has different prefixes or extra subnets; "
                    "it will not be readdressed"
                )
            continue
        for prefix in vnet.get("addressSpace", {}).get("addressPrefixes", []):
            other = ipaddress.ip_network(prefix)
            if other.version == 4 and config.network.address_prefix.overlaps(other):
                raise ValueError("the proposed portal address space overlaps an existing VNet")
    tags = {"application": "citadel-portal", "managedBy": "citadel-installer"}
    if command(
        ["group", "exists", "--subscription", config.subscription_id, "--name", config.resource_group_name]
    ):
        group = command(
            ["group", "show", "--subscription", config.subscription_id, "--name", config.resource_group_name]
        )
        tags = {**(group.get("tags") or {}), **tags}
        resources = command(
            [
                "resource",
                "list",
                "--subscription",
                config.subscription_id,
                "--resource-group",
                config.resource_group_name,
            ]
        )
        references = {
            web["serverFarmId"].lower(),
            config.resource_id(config.web_app, "Microsoft.Web/sites").lower(),
            config.resource_id(
                config.bff_identity, "Microsoft.ManagedIdentity/userAssignedIdentities"
            ).lower(),
        }
        for resource in resources:
            if (
                resource["id"].lower() in references
                or resource["type"].lower() == "microsoft.resources/deployments"
            ):
                continue
            if (resource.get("tags") or {}).get("managedBy") != "citadel-installer":
                raise ValueError(
                    "the target hosting group contains unrelated resources; choose a dedicated group"
                )
    return {
        "location": location,
        "origin": origin,
        "web": web,
        "bffIdentity": identity["properties"],
        "tags": tags,
    }


def _https_host(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.path not in ("", "/"):
        raise ValueError("resource connections must use the resource HTTPS origin")
    return (parsed.hostname or "").lower()


def _verify_entra(
    config: HostingInstallConfig, origin: str, identity: dict[str, Any], command: Command
) -> None:
    api = command(["ad", "app", "show", "--id", config.entra.api_client_id])
    client = (
        api
        if config.entra.client_id == config.entra.api_client_id
        else command(["ad", "app", "show", "--id", config.entra.client_id])
    )
    if api.get("appId", "").lower() != config.entra.api_client_id.lower() or (
        client.get("appId", "").lower() != config.entra.client_id.lower()
    ):
        raise ValueError("Entra configuration requires application client IDs, not directory object IDs")
    if api.get("signInAudience") != "AzureADMyOrg" or client.get("signInAudience") != "AzureADMyOrg":
        raise ValueError("customer API and confidential-client registrations must be single-tenant")
    if api.get("api", {}).get("requestedAccessTokenVersion") != 2:
        raise ValueError("the API registration must request v2 access tokens")
    if origin + "/auth/callback" not in client.get("web", {}).get("redirectUris", []):
        raise ValueError("the supplied client registration is missing the exact portal HTTPS callback")
    expected_scope = config.entra.delegated_scope_name
    if not any(
        item.get("value") == expected_scope and item.get("isEnabled") is True
        for item in api.get("api", {}).get("oauth2PermissionScopes", [])
    ):
        raise ValueError("the API registration is missing the enabled delegated scope")
    app_uri = f"api://{config.entra.api_client_id}"
    if app_uri not in api.get("identifierUris", []):
        raise ValueError("the API must expose its api://<API-client-ID> application ID URI")
    service = command(["ad", "sp", "show", "--id", config.entra.api_client_id])
    role = next(
        (
            item
            for item in service.get("appRoles", [])
            if item.get("value") == "Portal.InvokeAgent"
            and item.get("isEnabled") is True
            and "Application" in item.get("allowedMemberTypes", [])
        ),
        None,
    )
    if role is None:
        raise ValueError("the API service principal is missing the application-only Portal.InvokeAgent role")
    expected_roles = {"Platform.Viewer", "Platform.FinOpsAnalyst", "Platform.Operator", "Platform.Approver"}
    user_roles = {
        item["value"]
        for item in service.get("appRoles", [])
        if item.get("isEnabled") and "User" in item.get("allowedMemberTypes", [])
    }
    if not expected_roles.issubset(user_roles):
        raise ValueError("the API service principal is missing required platform user roles")
    uri = f"https://graph.microsoft.com/v1.0/servicePrincipals/{identity['principalId']}/appRoleAssignments"
    for _ in range(20):
        parsed = urlsplit(uri)
        if parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com":
            raise ValueError("unexpected Graph pagination target")
        result = command(["rest", "--method", "get", "--url", uri])
        if any(
            item.get("resourceId") == service["id"] and item.get("appRoleId") == role["id"]
            for item in result.get("value", [])
        ):
            return
        uri = result.get("@odata.nextLink")
        if not uri:
            break
    raise ValueError(
        "a tenant administrator must assign Portal.InvokeAgent to the supplied BFF managed identity "
        "before installation; Azure RBAC does not grant this application role"
    )


def runtime_settings(config: HostingInstallConfig, origin: str) -> dict[str, str]:
    app_uri = f"api://{config.entra.api_client_id}"
    hub = config.hub.model_dump(mode="json", by_alias=True)
    for grant in hub["initialGrants"]:
        grant["permissions"].sort()
    hub["initialGrants"].sort(key=lambda grant: grant["subjectId"])
    return {
        "CITADEL_PORTAL_TENANT_ID": config.hub.scope.tenant_id,
        "CITADEL_PORTAL_CLIENT_ID": config.entra.client_id,
        "CITADEL_PORTAL_API_AUDIENCE": config.entra.api_client_id,
        "CITADEL_PORTAL_DELEGATED_SCOPE": f"{app_uri}/{config.entra.delegated_scope_name}",
        "CITADEL_PORTAL_DELEGATED_SCOPE_NAME": config.entra.delegated_scope_name,
        "CITADEL_PORTAL_CLIENT_SECRET_NAME": config.entra.client_secret_name,
        "CITADEL_PORTAL_KEY_VAULT_URL": f"https://{config.key_vault.name}.vault.azure.net",
        "CITADEL_PORTAL_PORTAL_ORIGIN": origin,
        "CITADEL_PORTAL_CALLBACK_URI": origin + "/auth/callback",
        "CITADEL_PORTAL_AGENT_API_AUDIENCE": config.entra.api_client_id,
        "CITADEL_PORTAL_AGENT_API_TOKEN_SCOPE": app_uri + "/.default",
        "CITADEL_PORTAL_HUB_CONFIG": json.dumps(hub, sort_keys=True, separators=(",", ":")),
    }


def template_parameters(config: HostingInstallConfig, resolved: dict[str, Any], image: str) -> dict[str, Any]:
    if not _IMAGE.fullmatch(image):
        raise ValueError("agent image must be an immutable public image pinned by SHA-256 digest")

    def ref(value: AzureResourceRef) -> dict[str, str]:
        return {"resourceGroupName": value.resource_group_name, "name": value.name}

    values = {
        "resourceGroupName": config.resource_group_name,
        "location": resolved["location"],
        "namePrefix": config.name_prefix,
        "connections": {
            "webApp": ref(config.web_app),
            "bffIdentity": ref(config.bff_identity),
            "keyVault": ref(config.key_vault),
            "usageAccount": ref(config.usage_account),
            "modelAccount": ref(config.model_account),
            "apim": ref(config.apim),
            "logs": ref(config.logs),
            "usageDatabase": config.hub.usage_database,
            "clientSecretName": config.entra.client_secret_name,
            "githubSecretName": config.hub.github_token_secret_name,
        },
        "network": config.network.model_dump(mode="json", by_alias=True),
        "agentSettings": runtime_settings(config, resolved["origin"]),
        "agentImage": image,
        "isZoneRedundant": config.is_zone_redundant,
        "cosmosMaxThroughput": config.cosmos_max_throughput,
        "shouldCreateGatewayPrivateEndpoint": config.should_create_gateway_private_endpoint,
        "tags": resolved["tags"],
    }
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {key: {"value": value} for key, value in values.items()},
    }


def verify_preview(config: HostingInstallConfig, preview: dict[str, Any]) -> None:
    if preview.get("status") != "Succeeded" or not isinstance(preview.get("changes"), list):
        raise ValueError("ARM what-if did not produce a complete successful preview")
    group = f"/subscriptions/{config.subscription_id}/resourceGroups/{config.resource_group_name}".lower()
    protected = {
        config.resource_id(ref, kind).lower()
        for ref, kind in (
            (config.web_app, "Microsoft.Web/sites"),
            (config.bff_identity, "Microsoft.ManagedIdentity/userAssignedIdentities"),
            (config.key_vault, "Microsoft.KeyVault/vaults"),
            (config.usage_account, "Microsoft.DocumentDB/databaseAccounts"),
            (config.model_account, "Microsoft.CognitiveServices/accounts"),
            (config.apim, "Microsoft.ApiManagement/service"),
            (config.logs, "Microsoft.OperationalInsights/workspaces"),
        )
    }
    for change in preview["changes"]:
        kind = change.get("changeType")
        resource_id = change.get("resourceId", "").lower()
        if kind in {"NoChange", "Ignore"}:
            continue
        if kind not in {"Create", "Modify"}:
            raise ValueError(
                "ARM preview includes deletion, unsupported or unexpanded changes; installation stopped"
            )
        if resource_id in protected:
            raise ValueError("ARM preview would modify an existing customer resource; installation stopped")
        if resource_id == group or resource_id.startswith(group + "/"):
            continue
        if "/providers/microsoft.authorization/roleassignments/" in resource_id and any(
            resource_id.startswith(parent + "/") for parent in protected
        ):
            continue
        usage = config.resource_id(config.usage_account, "Microsoft.DocumentDB/databaseAccounts").lower()
        if resource_id.startswith(usage + "/sqlroleassignments/"):
            continue
        raise ValueError(
            "ARM preview includes a change outside the dedicated hosting and scoped access resources"
        )


def verify_bundle(bundle: Path, *, expected_image: str | None = None) -> None:
    manifest = json.loads(bundle.with_name("hosted-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("file") != bundle.name or hashlib.sha256(bundle.read_bytes()).hexdigest() != manifest.get(
        "sha256"
    ):
        raise ValueError("hosted artifact does not match its publication manifest")
    if expected_image is not None and (
        manifest.get("imagePublished") is not True or manifest.get("agentImage") != expected_image
    ):
        raise ValueError("agent image must match the published digest recorded by this artifact run")


def install(
    config: HostingInstallConfig,
    *,
    template: Path,
    image: str,
    bundle: Path,
    apply: bool = False,
    command: Command = az_json,
    deploy_application: Callable | None = None,
) -> dict[str, Any]:
    verify_bundle(bundle, expected_image=image)
    if not _IMAGE.fullmatch(image):
        raise ValueError("agent image must be an immutable public image pinned by SHA-256 digest")
    if not template.is_file():
        raise ValueError("the reviewed private-network Bicep template is missing")
    if apply and deploy_application is None:
        raise ValueError("apply requires an application deployment callback; no infrastructure was changed")
    resolved = preflight(config, command)
    parameters = template_parameters(config, resolved, image)
    with TemporaryDirectory(prefix="citadel-install-") as directory:
        path = Path(directory) / "parameters.json"
        path.write_text(json.dumps(parameters), encoding="utf-8")
        base = [
            "--subscription",
            config.subscription_id,
            "--location",
            resolved["location"],
            "--name",
            f"citadel-{config.name_prefix}",
            "--template-file",
            str(template.resolve()),
            "--parameters",
            "@" + str(path.resolve()),
        ]
        validation = command(["deployment", "sub", "validate", *base])
        if validation.get("error"):
            raise ValueError("Azure provider validation failed before infrastructure deployment")
        preview = command(
            [
                "deployment",
                "sub",
                "what-if",
                *base,
                "--no-pretty-print",
                "--result-format",
                "FullResourcePayloads",
            ]
        )
        verify_preview(config, preview)
        print(
            "Preflight and ARM preview passed. "
            "Existing gateway, data/model accounts and registration are retained."
        )
        if not apply:
            print("Preview only: no Azure resources or host configuration changed.")
            return {
                "status": "previewed",
                "location": resolved["location"],
                "changes": len(preview["changes"]),
            }
        result = command(["deployment", "sub", "create", *base])
    properties = result.get("properties", {})
    if properties.get("provisioningState") != "Succeeded":
        raise RuntimeError("hosting deployment did not succeed; inspect the ARM deployment before retrying")
    hosts = properties["outputs"]["hosts"]["value"]
    _verify_private_connections(config, command)
    _configure_bff(config, resolved, hosts, command)
    deploy_application(config, hosts, image, bundle, command)
    print(
        "Private infrastructure and application deployment completed. "
        "Authenticated customer acceptance is still required."
    )
    return {"status": "installed", "portalOrigin": resolved["origin"], "hosts": hosts}


def _verify_private_connections(config: HostingInstallConfig, command: Command) -> None:
    endpoints = command(
        [
            "network",
            "private-endpoint",
            "list",
            "--subscription",
            config.subscription_id,
            "--resource-group",
            config.resource_group_name,
        ]
    )
    required = {"state", "usage", "vault", "model"}
    if config.should_create_gateway_private_endpoint:
        required.add("gateway")
    by_name = {item["name"]: item for item in endpoints}
    for suffix in required:
        endpoint = by_name.get(f"pe-{config.name_prefix}-{suffix}", {})
        connections = endpoint.get("privateLinkServiceConnections", [])
        if not connections or any(
            connection.get("privateLinkServiceConnectionState", {}).get("status") != "Approved"
            for connection in connections
        ):
            raise RuntimeError(
                f"the {suffix} private endpoint is missing or not approved; "
                "ask its resource owner to approve the connection, then rerun the installer"
            )


def _configure_bff(
    config: HostingInstallConfig, resolved: dict[str, Any], hosts: dict[str, Any], command: Command
) -> None:
    common = [
        "--subscription",
        config.subscription_id,
        "--resource-group",
        config.web_app.resource_group_name,
        "--name",
        config.web_app.name,
    ]
    command(["webapp", "identity", "assign", *common, "--identities", hosts["bffIdentityId"]])
    command(
        [
            "webapp",
            "vnet-integration",
            "add",
            *common,
            "--vnet",
            hosts["bffSubnetId"].rsplit("/subnets/", 1)[0],
            "--subnet",
            "bff",
        ]
    )
    settings = runtime_settings(config, resolved["origin"])
    settings.pop("CITADEL_PORTAL_HUB_CONFIG")
    settings.update(
        {
            "CITADEL_PORTAL_MANAGED_IDENTITY_CLIENT_ID": hosts["bffIdentityClientId"],
            "CITADEL_PORTAL_BFF_SERVICE_CLIENT_ID": hosts["bffIdentityClientId"],
            "CITADEL_PORTAL_AGENT_API_URL": hosts["agentApiUrl"],
            "CITADEL_PORTAL_COSMOS_ENDPOINT": hosts["stateEndpoint"],
            "CITADEL_PORTAL_COSMOS_DATABASE": hosts["stateDatabase"],
            "CITADEL_PORTAL_COSMOS_CONTAINER": hosts["stateContainer"],
        }
    )
    with TemporaryDirectory(prefix="citadel-bff-settings-") as directory:
        path = Path(directory) / "settings.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        command(["webapp", "config", "appsettings", "set", *common, "--settings", "@" + str(path)])
        web_id = config.resource_id(config.web_app, "Microsoft.Web/sites")
        patch = Path(directory) / "https.json"
        patch.write_text(json.dumps({"properties": {"httpsOnly": True}}), encoding="utf-8")
        command(
            [
                "rest",
                "--method",
                "patch",
                "--url",
                f"https://management.azure.com{web_id}?api-version=2024-11-01",
                "--body",
                "@" + str(patch),
            ]
        )
    command(
        [
            "webapp",
            "config",
            "set",
            *common,
            "--min-tls-version",
            "1.2",
            "--ftps-state",
            "Disabled",
            "--vnet-route-all-enabled",
            "true",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path)
    source.add_argument(
        "--config-env", help="Name of the environment variable containing non-secret installation JSON."
    )
    parser.add_argument("--template", type=Path, default=Path("infra") / "main.bicep")
    parser.add_argument("--image", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--expected-subscription", help="Subscription pinned by the protected workflow environment."
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        raw = args.config.read_text(encoding="utf-8") if args.config else os.environ[args.config_env]
        config = HostingInstallConfig.model_validate_json(raw)
        if (
            args.expected_subscription
            and args.expected_subscription.lower() != config.subscription_id.lower()
        ):
            raise ValueError("installation JSON targets a different subscription from the protected workflow")
        install(
            config,
            template=args.template,
            image=args.image,
            bundle=args.bundle,
            apply=args.apply,
            deploy_application=deploy_installed_application,
        )
    except ValidationError as exc:
        locations = ", ".join(".".join(str(item) for item in error["loc"]) for error in exc.errors())
        logger.error("Installation configuration is invalid at: %s. Values are not logged.", locations)
        return 2
    except (ValueError, KeyError, CommandError, OSError, RuntimeError) as exc:
        logger.error(
            "Installation stopped (%s). No automatic rollback or permission escalation was attempted.",
            type(exc).__name__,
        )
        logger.error("%s", str(exc))
        return 1
    return 0


def deploy_installed_application(
    config: HostingInstallConfig, hosts: dict[str, Any], image: str, bundle: Path, command: Command
) -> None:
    from citadel_agent.portal.delivery import deploy

    args = argparse.Namespace(
        subscription=config.subscription_id,
        web_resource_group=config.web_app.resource_group_name,
        web_app=config.web_app.name,
        agent_resource_group=hosts["agentResourceGroup"],
        agent_app=hosts["agentApp"],
        agent_image=image,
        bundle=bundle,
        apply=True,
    )
    deploy(args, command=command)


if __name__ == "__main__":
    sys.exit(main())
