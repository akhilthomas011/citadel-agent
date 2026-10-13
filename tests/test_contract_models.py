# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for contract intent models and YAML round-tripping."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from citadel_agent.contracts import (
    AccessContractIntent,
    AlertsIntent,
    BackendContractIntent,
    PublishContractIntent,
    dump_intent,
    intent_path,
    load_intent,
    parse_intent,
    save_intent,
)

SUB = "11111111-1111-1111-1111-111111111111"


def access_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "access-contract",
        "environment": "dev",
        "metadata": {"owners": ["Dev@Contoso.com"], "description": "HR chatbot"},
        "useCase": {"businessUnit": "HR", "useCaseName": "ChatAgent", "environment": "DEV"},
        "services": [
            {
                "code": "LLM",
                "apis": ["universal-llm-api", "azure-openai-api", "unified-ai-api"],
                "keySecretName": "HR-CHATAGENT-LLM-KEY",
                "endpointSecretName": "HR-CHATAGENT-LLM-ENDPOINT",
                "policy": {
                    "allowedModels": ["gpt-5.2", "text-embedding-3-large"],
                    "tokenLimit": {
                        "tokensPerMinute": 5000,
                        "tokenQuota": 500000,
                        "tokenQuotaPeriod": "Monthly",
                    },
                },
            }
        ],
    }
    data.update(overrides)
    return data


def backend_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "backend-contract",
        "environment": "dev",
        "backends": [
            {
                "backendId": "aif-0",
                "backendType": "ai-foundry",
                "endpoint": "https://aif-0.cognitiveservices.azure.com/",
                "supportedModels": [{"name": "gpt-5.2"}, {"name": "Mistral-Large-3"}],
                "priority": 1,
            },
            {
                "backendId": "aif-1",
                "backendType": "ai-foundry",
                "endpoint": "https://aif-1.cognitiveservices.azure.com/",
                "supportedModels": [{"name": "gpt-5.2"}, {"name": "gpt-5.4-mini"}],
                "priority": 2,
            },
        ],
    }
    data.update(overrides)
    return data


# Access contracts ------------------------------------------------------------------------------


def test_access_contract_naming_matches_upstream_conventions() -> None:
    intent = AccessContractIntent.model_validate(access_data())
    assert intent.product_id("LLM") == "LLM-HR-ChatAgent-DEV"
    assert intent.product_display_name("LLM") == "LLM HR ChatAgent DEV"
    assert intent.subscription_name("LLM") == "LLM-HR-ChatAgent-DEV-SUB-01"
    assert intent.foundry_connection_name("LLM") == "Hub-HR-ChatAgent-DEV-LLM"
    assert intent.metadata.owners == ["dev@contoso.com"]


def test_duplicate_service_codes_are_rejected() -> None:
    data = access_data()
    data["services"] = data["services"] * 2
    with pytest.raises(ValidationError, match="only once"):
        AccessContractIntent.model_validate(data)


def test_long_names_that_break_nested_deployment_names_are_rejected() -> None:
    data = access_data(
        useCase={"businessUnit": "B" * 20, "useCaseName": "U" * 25, "environment": "DEV"},
        resiliency={
            "additionalApimGateways": [{"subscriptionId": SUB, "resourceGroupName": "rg", "name": "apim-2"}]
        },
    )
    with pytest.raises(ValidationError, match="64"):
        AccessContractIntent.model_validate(data)


def test_literal_secret_in_any_field_is_rejected() -> None:
    data = access_data(productTerms="key 0123456789abcdef0123456789abcdef")
    with pytest.raises(ValidationError, match="must not be stored"):
        AccessContractIntent.model_validate(data)


def test_whitespace_in_allowed_models_is_rejected() -> None:
    data = access_data()
    data["services"][0]["policy"]["allowedModels"] = ["gpt-5.2, gpt-4o"]
    with pytest.raises(ValidationError):
        AccessContractIntent.model_validate(data)


def test_model_token_limits_must_reference_allowed_models() -> None:
    data = access_data()
    data["services"][0]["policy"]["modelTokenLimits"] = [{"model": "gpt-4o", "tokensPerMinute": 100}]
    with pytest.raises(ValidationError, match="not allowed"):
        AccessContractIntent.model_validate(data)


def test_tool_limits_require_multi_asset_code() -> None:
    data = access_data()
    data["services"][0]["policy"]["toolLimits"] = {"calls": 60}
    with pytest.raises(ValidationError, match="TOOL, AGENT or MULTI"):
        AccessContractIntent.model_validate(data)


def test_multi_asset_contract_with_tool_limits() -> None:
    data = access_data(
        services=[
            {
                "code": "MULTI",
                "apis": ["universal-llm-api", "weather-tool"],
                "keySecretName": "HR-MULTI-KEY",
                "assetEndpoints": [{"apiName": "weather-tool"}],
                "foundryApiName": "universal-llm-api",
                "policy": {"toolLimits": {"calls": 60, "quotaCalls": 100000, "quotaPeriodSeconds": 2592000}},
            }
        ]
    )
    intent = AccessContractIntent.model_validate(data)
    assert intent.services[0].is_multi_asset
    assert intent.services[0].endpoint_secret_count == 1


def test_asset_endpoint_must_reference_granted_api() -> None:
    data = access_data()
    data["services"][0]["assetEndpoints"] = [{"apiName": "not-granted"}]
    with pytest.raises(ValidationError, match="not granted"):
        AccessContractIntent.model_validate(data)


@pytest.mark.parametrize(
    ("resiliency", "message"),
    [
        (
            {
                "additionalKeyVaults": [
                    {
                        "subscriptionId": SUB,
                        "resourceGroupName": "rg",
                        "name": "kv",
                        "endpointSource": "global",
                    }
                ]
            },
            "globalGatewayUrl",
        ),
        (
            {
                "additionalKeyVaults": [
                    {
                        "subscriptionId": SUB,
                        "resourceGroupName": "rg",
                        "name": "kv",
                        "endpointSource": "secondary:0",
                    }
                ]
            },
            "points past",
        ),
        (
            {
                "additionalKeyVaults": [
                    {
                        "subscriptionId": SUB,
                        "resourceGroupName": "rg",
                        "name": "kv",
                        "endpointSource": "bogus",
                    }
                ]
            },
            "Invalid endpointSource",
        ),
    ],
)
def test_resiliency_endpoint_sources(resiliency: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        AccessContractIntent.model_validate(access_data(resiliency=resiliency))


def test_foundry_connection_requires_project_and_forbids_credential_headers() -> None:
    with pytest.raises(ValidationError, match="Foundry project is required"):
        AccessContractIntent.model_validate(access_data(foundry={"enabled": True}))
    with pytest.raises(ValidationError, match="customHeaders"):
        AccessContractIntent.model_validate(access_data(foundry={"customHeaders": {"api-key": "x"}}))


def test_usage_dimension_needs_exactly_one_source() -> None:
    data = access_data()
    data["services"][0]["policy"]["customDimension1"] = {"header": "x-enduser-id", "staticValue": "a"}
    with pytest.raises(ValidationError, match="exactly one"):
        AccessContractIntent.model_validate(data)


def test_unknown_fields_are_rejected_in_agent_owned_structures() -> None:
    data = access_data()
    data["services"][0]["policy"]["typoField"] = True
    with pytest.raises(ValidationError):
        AccessContractIntent.model_validate(data)


# Backend contracts -----------------------------------------------------------------------------


def test_backend_contract_pools_and_unknown_upstream_fields_are_preserved() -> None:
    data = backend_data()
    data["backends"][0]["futureUpstreamField"] = {"x": 1}
    data["backends"][0]["supportedModels"][0]["newModelSetting"] = "kept"
    intent = BackendContractIntent.model_validate(data)
    assert intent.pooled_models() == {"gpt-5.2": ["aif-0", "aif-1"]}
    dumped = intent.model_dump(by_alias=True, exclude_none=True)
    assert dumped["backends"][0]["futureUpstreamField"] == {"x": 1}
    assert dumped["backends"][0]["supportedModels"][0]["newModelSetting"] == "kept"


def test_api_key_backend_requires_key_vault_reference() -> None:
    data = backend_data()
    data["backends"].append(
        {
            "backendId": "gemini",
            "backendType": "gemini-openai",
            "endpoint": "https://generativelanguage.googleapis.com/",
            "authType": "api-key-bearer",
            "authConfig": {"namedValueKey": "gemini-key"},
            "supportedModels": [{"name": "gemini-2.5-flash"}],
        }
    )
    with pytest.raises(ValidationError, match="keyVaultSecretUri"):
        BackendContractIntent.model_validate(data)


def test_inline_backend_secret_and_redacted_values_are_rejected() -> None:
    for secret_value in ("plain-secret", "***redacted***"):
        data = backend_data()
        data["backends"][0]["authConfig"] = {"namedValueKey": "k", "secretValue": secret_value}
        with pytest.raises(ValidationError):
            BackendContractIntent.model_validate(data)


@pytest.mark.parametrize(
    ("alias", "message"),
    [
        ({"name": "gpt-5.2", "models": ["gpt-5.4-mini"]}, "collides"),
        ({"name": "smart", "models": ["gpt-9"]}, "unknown model"),
        (
            {
                "name": "smart",
                "models": ["gpt-5.2", "gpt-5.4-mini"],
                "strategy": "weighted",
                "weights": [100],
            },
            "one weight per model",
        ),
        ({"name": "smart", "models": ["gpt-5.2"], "weights": [1]}, "only valid with the weighted"),
    ],
)
def test_alias_validation(alias: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        BackendContractIntent.model_validate(backend_data(modelAliases=[alias]))


def test_alias_cannot_mix_wire_protocols() -> None:
    data = backend_data()
    data["backends"].append(
        {
            "backendId": "claude",
            "backendType": "anthropic",
            "endpoint": "https://api.anthropic.com/",
            "authType": "api-key-anthropic",
            "authConfig": {
                "namedValueKey": "anthropic-key",
                "keyVaultSecretUri": "https://kv.vault.azure.net/secrets/a",
            },
            "supportedModels": [{"name": "claude-sonnet-4-6"}],
        }
    )
    data["modelAliases"] = [{"name": "mixed", "models": ["gpt-5.2", "claude-sonnet-4-6"]}]
    with pytest.raises(ValidationError, match="mixes wire protocols"):
        BackendContractIntent.model_validate(data)


def test_bedrock_requires_pipeline_credentials_and_flux_requires_model_path() -> None:
    bedrock = backend_data()
    bedrock["backends"].append(
        {
            "backendId": "bedrock",
            "backendType": "aws-bedrock",
            "endpoint": "https://bedrock-runtime.us-east-1.amazonaws.com",
            "authType": "aws-sigv4",
            "supportedModels": [{"name": "us.anthropic.claude-3-5-haiku-20241022-v1:0"}],
        }
    )
    with pytest.raises(ValidationError, match="pipeline-secret"):
        BackendContractIntent.model_validate(bedrock)
    BackendContractIntent.model_validate(dict(bedrock, awsCredentialsSource="pipeline-secret"))

    flux = backend_data()
    flux["backends"].append(
        {
            "backendId": "flux",
            "backendType": "azure-flux",
            "endpoint": "https://aif-0.services.ai.azure.com/",
            "supportedModels": [{"name": "FLUX.2-pro"}],
        }
    )
    with pytest.raises(ValidationError, match="modelPath"):
        BackendContractIntent.model_validate(flux)


# Publish contracts -----------------------------------------------------------------------------


def publish_data(assets: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    return {"kind": "publish-contract", "environment": "dev", "name": "tools", "assets": assets, **overrides}


MCP_EXISTING = {
    "assetType": "mcp-existing",
    "name": "ms-learn-tool",
    "displayName": "Microsoft Learn Tool (MCP)",
    "path": "ms-learn-tool-mcp",
    "transportType": "streamable",
    "backend": {"url": "https://learn.microsoft.com/api/mcp", "authType": "none"},
}
MCP_FROM_API = {
    "assetType": "mcp-from-api",
    "name": "weather-tool",
    "displayName": "Weather Tool (MCP)",
    "path": "weather-tool-mcp",
    "sourceApiName": "weather-api",
    "operationNames": ["get-weather"],
}


def test_publish_gateway_paths_follow_prefix_rules() -> None:
    intent = PublishContractIntent.model_validate(publish_data([MCP_EXISTING, MCP_FROM_API]))
    existing, from_api = intent.assets
    assert existing.gateway_path() == "mcp/ms-learn-tool-mcp"
    assert from_api.gateway_path() == "mcp/weather-tool-mcp/mcp"
    assert from_api.gateway_path(use_asset_type_path_prefix=False) == "weather-tool-mcp/mcp"
    opted_out = PublishContractIntent.model_validate(publish_data([dict(MCP_EXISTING, pathPrefix="")]))
    assert opted_out.assets[0].gateway_path() == "ms-learn-tool-mcp"


@pytest.mark.parametrize(
    ("asset", "message"),
    [
        (dict(MCP_FROM_API, operationNames=[]), "sourceApiName and operationNames"),
        ({k: v for k, v in MCP_EXISTING.items() if k != "backend"}, "needs a backend"),
        ({"assetType": "a2a", "name": "agent", "displayName": "A", "path": "agent"}, "a2a needs"),
        (
            dict(MCP_EXISTING, backend={"url": "https://x.example", "authType": "managed-identity"}),
            "authConfig.resource",
        ),
    ],
)
def test_publish_asset_type_requirements(asset: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        PublishContractIntent.model_validate(publish_data([asset]))


def test_publish_to_api_center_requires_service() -> None:
    with pytest.raises(ValidationError, match=r"apiCenter\.serviceName"):
        PublishContractIntent.model_validate(publish_data([dict(MCP_EXISTING, publishToApiCenter=True)]))


# Alerts ----------------------------------------------------------------------------------------


def test_alerts_defaults_match_upstream() -> None:
    intent = AlertsIntent.model_validate(
        {
            "kind": "alerts",
            "environment": "dev",
            "scopeName": "gateway",
            "alertEmailAddress": "ops@contoso.com",
        }
    )
    assert [a.name for a in intent.alerts] == ["throttling", "quota-exceeded", "backend-failure"]


def test_alerts_product_filter_must_be_display_name() -> None:
    with pytest.raises(ValidationError, match="DISPLAY name"):
        AlertsIntent.model_validate(
            {
                "kind": "alerts",
                "environment": "dev",
                "scopeName": "hr",
                "alertEmailAddress": "ops@contoso.com",
                "productDisplayNameFilter": "LLM-HR-ChatAgent-DEV",
            }
        )


# YAML round trip -------------------------------------------------------------------------------


def test_yaml_round_trip_and_repository_location(tmp_path: Path) -> None:
    intent = parse_intent(access_data())
    text = dump_intent(intent)
    assert "useCase:" in text and "keySecretName:" in text

    path = save_intent(intent, tmp_path / "contract.yaml")
    assert load_intent(path) == intent
    assert str(intent_path(intent)) == "contracts/access/hr/chatagent/dev/contract.yaml"
    assert str(intent_path(parse_intent(backend_data()))) == "contracts/backend/dev/contract.yaml"
