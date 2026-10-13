# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Discover a deployed Citadel hub's coordinates and emit an admin-policy environment block."""

from __future__ import annotations

from typing import Any

from citadel_agent.azure_clients.arm import ArmClient

APIM_TYPE = "Microsoft.ApiManagement/service"
IDENTITY_TYPE = "Microsoft.ManagedIdentity/userAssignedIdentities"
KEY_VAULT_TYPE = "Microsoft.KeyVault/vaults"
APP_INSIGHTS_TYPE = "Microsoft.Insights/components"
LOG_ANALYTICS_TYPE = "Microsoft.OperationalInsights/workspaces"
COSMOS_TYPE = "Microsoft.DocumentDB/databaseAccounts"

_API_VERSIONS = {
    APIM_TYPE: "2024-05-01",
    IDENTITY_TYPE: "2023-01-31",
    LOG_ANALYTICS_TYPE: "2022-10-01",
}


class DiscoveryError(RuntimeError):
    """Raised when the resource group does not look like a Citadel hub."""


def _pick(
    resources: list[dict[str, Any]], resource_type: str, prefix: str | None = None
) -> dict[str, Any] | None:
    candidates = [r for r in resources if str(r.get("type", "")).lower() == resource_type.lower()]
    if prefix:
        preferred = [r for r in candidates if str(r.get("name", "")).startswith(prefix)]
        candidates = preferred or candidates
    return candidates[0] if candidates else None


def discover_environment(
    arm: ArmClient,
    subscription_id: str,
    resource_group: str,
    *,
    environment_class: str = "dev",
    mode: str = "direct",
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Return an admin-policy environment entry (camelCase) for the hub in ``resource_group``."""
    resources = arm.list_resources(subscription_id, resource_group)
    apim = _pick(resources, APIM_TYPE)
    if apim is None:
        raise DiscoveryError(f"No API Management instance found in {resource_group}")
    apim_details = arm.get(apim["id"], _API_VERSIONS[APIM_TYPE])

    def ref(resource: dict[str, Any] | None) -> dict[str, str] | None:
        if resource is None:
            return None
        return {
            "subscriptionId": subscription_id,
            "resourceGroupName": resource_group,
            "name": resource["name"],
        }

    environment: dict[str, Any] = {
        "class": environment_class,
        "mode": mode if environment_class != "prod" else "pr",
        "subscriptionId": subscription_id,
        "location": apim_details.get("location", "").replace(" ", "").lower(),
        "apim": ref(apim),
        "gatewayUrl": apim_details.get("properties", {}).get("gatewayUrl"),
    }
    if tenant_id:
        environment["tenantId"] = tenant_id

    identity = _pick(resources, IDENTITY_TYPE, prefix="id-apim-")
    if identity is not None:
        details = arm.get(identity["id"], _API_VERSIONS[IDENTITY_TYPE])
        environment["apimManagedIdentity"] = {
            **(ref(identity) or {}),
            "clientId": details.get("properties", {}).get("clientId"),
        }
    for key, resource_type, prefix in (
        ("keyVault", KEY_VAULT_TYPE, "kv-"),
        ("appInsights", APP_INSIGHTS_TYPE, "appi-apim-"),
    ):
        found = ref(_pick(resources, resource_type, prefix))
        if found:
            environment[key] = found
    workspace = _pick(resources, LOG_ANALYTICS_TYPE)
    if workspace is not None:
        details = arm.get(workspace["id"], _API_VERSIONS[LOG_ANALYTICS_TYPE])
        customer_id = details.get("properties", {}).get("customerId")
        if customer_id:
            environment["logAnalyticsWorkspaceId"] = customer_id
    cosmos = _pick(resources, COSMOS_TYPE)
    if cosmos is not None:
        environment["cosmos"] = {"accountName": cosmos["name"]}
    return environment
