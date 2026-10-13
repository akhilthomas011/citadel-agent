# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Import existing Citadel parameter values into contract intents.

Used to seed the canonical repository from what already exists:

* the hub's initial backend configuration (``LLM_BACKEND_CONFIG`` deployment output, or the
  ``backends`` section of ``GET /version/backend-contract`` when it carries no inline secrets)
* hand-written ``.bicepparam`` files, compiled with ``bicep build-params`` (ARM parameter JSON)

Product policies of imported access contracts are kept verbatim (``policyXmlFile``) because their
semantics may go beyond what the policy intent can express.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from citadel_agent.contracts.access import AccessContractIntent
from citadel_agent.contracts.alerts import AlertsIntent
from citadel_agent.contracts.backend import BackendContractIntent
from citadel_agent.contracts.common import ContractMetadata
from citadel_agent.contracts.publish import PublishContractIntent


def unwrap(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Turn an ARM parameters object (``{"name": {"value": ...}}``) into plain values."""
    if "parameters" in parameters and isinstance(parameters["parameters"], Mapping):
        parameters = parameters["parameters"]
    return {
        name: (entry.get("value") if isinstance(entry, Mapping) and "value" in entry else entry)
        for name, entry in parameters.items()
    }


def backend_intent_from_config(
    llm_backend_config: list[Mapping[str, Any]],
    environment: str,
    *,
    model_aliases: list[Mapping[str, Any]] | None = None,
    metadata: ContractMetadata | None = None,
) -> BackendContractIntent:
    """Build the backend intent from an ``llmBackendConfig`` array (raises on inline secrets)."""
    return BackendContractIntent.model_validate(
        {
            "environment": environment,
            "metadata": (metadata or ContractMetadata()).model_dump(by_alias=True),
            "backends": [dict(entry) for entry in llm_backend_config],
            "modelAliases": [dict(alias) for alias in model_aliases or []],
        }
    )


def backend_intent_from_parameters(parameters: Mapping[str, Any], environment: str) -> BackendContractIntent:
    values = unwrap(parameters)
    data: dict[str, Any] = {
        "environment": environment,
        "backends": values.get("llmBackendConfig", []),
        "modelAliases": values.get("modelAliases", []),
        "circuitBreakerEnabled": values.get("configureCircuitBreaker", True),
        "sessionAffinityEnabled": values.get("configureSessionAffinity", True),
    }
    if values.get("circuitBreakerDefaults"):
        data["circuitBreakerDefaults"] = values["circuitBreakerDefaults"]
    if values.get("sessionAffinityDefaults"):
        data["sessionAffinityDefaults"] = values["sessionAffinityDefaults"]
    if values.get("anthropicVersion"):
        data["anthropicVersion"] = values["anthropicVersion"]
    if any(b.get("backendType") == "aws-bedrock" for b in data["backends"]):
        data["awsCredentialsSource"] = "pipeline-secret"
    return BackendContractIntent.model_validate(data)


def access_intent_from_parameters(
    parameters: Mapping[str, Any],
    environment: str,
    *,
    owners: list[str] | None = None,
) -> tuple[AccessContractIntent, dict[str, str]]:
    """Build an access intent from compiled parameters.

    Returns:
        The intent and the product-policy XML per policy file name (to be written next to the
        intent, since imported policies are kept verbatim).
    """
    values = unwrap(parameters)
    mapping: Mapping[str, list[str]] = values.get("apiNameMapping", {})
    services = []
    policies: dict[str, str] = {}
    for item in values.get("services", []):
        code = item["code"]
        service: dict[str, Any] = {
            "code": code,
            "apis": list(mapping.get(code, [])),
            "keySecretName": item["apiKeySecretName"],
        }
        for source, target in (
            ("endpointSecretName", "endpointSecretName"),
            ("assetEndpoints", "assetEndpoints"),
            ("publishAllAssetEndpoints", "publishAllAssetEndpoints"),
            ("foundryApiName", "foundryApiName"),
        ):
            if item.get(source):
                service[target] = item[source]
        policy_xml = item.get("policyXml")
        if policy_xml:
            file_name = f"{code.lower()}-imported-policy.xml"
            policies[file_name] = policy_xml
            service["policyXmlFile"] = file_name
        services.append(service)

    foundry_enabled = bool(values.get("useTargetFoundry"))
    config = values.get("foundryConfig") or {}
    data: dict[str, Any] = {
        "environment": environment,
        "metadata": {"owners": owners or []},
        "useCase": values["useCase"],
        "services": services,
        "storeCredentialsInKeyVault": bool(values.get("useTargetAzureKeyVault", True)),
        "productTerms": values.get("productTerms", ""),
        "keyRotation": {
            "usePrimaryKey": values.get("usePrimaryKey", True),
            "keyRotationEnabled": False,
        },
    }
    if foundry_enabled:
        data["foundry"] = {
            "enabled": True,
            "project": values.get("foundry"),
            "connectionNamePrefix": config.get("connectionNamePrefix", ""),
            "connectionCategory": config.get("connectionCategory", "ApiManagement"),
            "authType": config.get("authType", "ProjectManagedIdentity"),
            "managedIdentityAudience": config.get(
                "managedIdentityAudience", "https://cognitiveservices.azure.com"
            ),
            "deploymentInPath": str(config.get("deploymentInPath", "false")).lower() == "true",
            "isSharedToAll": bool(config.get("isSharedToAll", False)),
            "inferenceApiVersion": config.get("inferenceAPIVersion", ""),
            "deploymentApiVersion": config.get("deploymentAPIVersion", ""),
            "staticModels": config.get("staticModels", []),
            "listModelsEndpoint": config.get("listModelsEndpoint", ""),
            "getModelEndpoint": config.get("getModelEndpoint", ""),
            "deploymentProvider": config.get("deploymentProvider", ""),
            "customHeaders": config.get("customHeaders", {}),
        }
    resiliency = {
        key: values[key]
        for key in (
            "globalGatewayUrl",
            "additionalApimGateways",
            "additionalKeyVaults",
            "additionalFoundries",
        )
        if values.get(key)
    }
    if resiliency:
        data["resiliency"] = resiliency
    return AccessContractIntent.model_validate(data), policies


def publish_intent_from_parameters(
    parameters: Mapping[str, Any], environment: str, name: str
) -> PublishContractIntent:
    values = unwrap(parameters)
    assets = []
    for asset in values.get("publishAssets", []):
        entry = dict(asset)
        if entry.pop("policyXml", None):
            raise ValueError(
                f"Asset '{entry.get('name')}' has an inline policyXml; save it to a file before importing"
            )
        assets.append(entry)
    data: dict[str, Any] = {
        "environment": environment,
        "name": name,
        "assets": assets,
        "configureCircuitBreaker": values.get("configureCircuitBreaker", True),
        "ensureUsageFragments": values.get("ensureUsageFragments", True),
        "useAssetTypePathPrefix": values.get("useAssetTypePathPrefix", True),
    }
    api_center = values.get("apiCenter")
    if api_center and api_center.get("serviceName"):
        data["apiCenter"] = api_center
    if values.get("circuitBreakerDefaults"):
        data["circuitBreakerDefaults"] = values["circuitBreakerDefaults"]
    return PublishContractIntent.model_validate(data)


def alerts_intent_from_parameters(
    parameters: Mapping[str, Any], environment: str, scope_name: str
) -> AlertsIntent:
    values = unwrap(parameters)
    return AlertsIntent.model_validate(
        {
            "environment": environment,
            "scopeName": scope_name,
            "alertEmailAddress": values["alertEmailAddress"],
            "additionalEmailReceivers": values.get("additionalEmailReceivers", []),
            "productDisplayNameFilter": values.get("productNameFilter", ""),
            "alertMode": values.get("alertMode", "logQuery"),
            "namePrefix": values.get("namePrefix"),
            "actionGroupShortName": values.get("actionGroupShortName", "aigwalerts"),
            "alertsEnabled": values.get("alertsEnabled", True),
            "alerts": values.get("alerts", []),
        }
    )
