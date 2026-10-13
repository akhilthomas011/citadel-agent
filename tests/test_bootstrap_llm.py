# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for environment discovery, the agent's bootstrap contract and its gateway LLM client."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from azure.core.exceptions import ResourceNotFoundError

from citadel_agent import llm_client
from citadel_agent.azure_clients.keyvault import KeyVaultAccessError
from citadel_agent.bootstrap import (
    AGENT_APIS,
    DEFAULT_AGENT_TPM,
    BootstrapError,
    agent_access_intent,
    bootstrap_request,
)
from citadel_agent.discovery import DiscoveryError, discover_environment
from citadel_agent.policy import Operation
from citadel_agent.policy.admin import parse_admin_policy
from citadel_agent.safety.redaction import REDACTED, default_redactor

SUB = "11111111-1111-1111-1111-111111111111"
RG = "rg-citadel-dev"


def _policy(*, max_tpm: int | None = None, agent: bool = True) -> Any:
    caps = f"\n    caps:\n      maxTokensPerMinute: {max_tpm}" if max_tpm else ""
    agent_block = (
        "\nagent:\n  environment: dev\n  model: gpt-5.2\n  gatewayKeySecretName: CITADEL-AGENT-LLM-KEY"
        if agent
        else ""
    )
    return parse_admin_policy(
        f"""
version: 1
defaultEnvironment: dev
environments:
  dev:
    class: dev
    mode: direct
    subscriptionId: {SUB}
    location: swedencentral
    apim: {{subscriptionId: {SUB}, resourceGroupName: {RG}, name: apim-dev}}
    gatewayUrl: https://apim-dev.azure-api.net
    keyVault: {{subscriptionId: {SUB}, resourceGroupName: {RG}, name: kv-dev}}{caps}
gitops:
  repository: contoso/citadel-contracts{agent_block}
"""
    )


# Bootstrap --------------------------------------------------------------------------------------


def test_agent_access_intent_uses_the_policy_agent_settings() -> None:
    intent = agent_access_intent(_policy(), "admin@contoso.com")
    service = intent.services[0]
    assert intent.environment == "dev"
    assert intent.metadata.owners == ["admin@contoso.com"]
    assert (intent.use_case.business_unit, intent.use_case.use_case_name) == ("Platform", "CitadelAgent")
    assert service.apis == AGENT_APIS
    assert service.key_secret_name == "CITADEL-AGENT-LLM-KEY"
    assert service.policy.allowed_models == ["gpt-5.2"]
    assert service.policy.token_limit.tokens_per_minute == DEFAULT_AGENT_TPM


def test_agent_access_intent_respects_environment_caps() -> None:
    intent = agent_access_intent(_policy(max_tpm=5000), "admin@contoso.com")
    assert intent.services[0].policy.token_limit.tokens_per_minute == 5000


def test_bootstrap_requires_the_agent_section() -> None:
    with pytest.raises(BootstrapError, match="'agent' section"):
        agent_access_intent(_policy(agent=False), "admin@contoso.com")


def test_bootstrap_request_is_a_single_access_change() -> None:
    request = bootstrap_request(_policy(), "admin@contoso.com")
    assert request.environment == "dev"
    assert [(c.step_id, c.operation) for c in request.changes] == [("agent-access", Operation.ACCESS_WRITE)]


# LLM client -------------------------------------------------------------------------------------


def _runtime(*, gateway_key: str | None = None, agent: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(gateway_key=gateway_key, model=None),
        policy=_policy(agent=agent),
        credential=object(),
    )


class FakeReader:
    behaviour: ClassVar[Any] = "gateway-key-from-vault-0123456789"
    requested: ClassVar[list[str]] = []

    def __init__(self, credential: Any, url: str) -> None:
        self.url = url

    def get_secret_in_memory(self, name: str) -> str:
        FakeReader.requested.append(name)
        if isinstance(FakeReader.behaviour, Exception):
            raise FakeReader.behaviour
        return FakeReader.behaviour


@pytest.fixture
def fake_reader(monkeypatch: pytest.MonkeyPatch) -> type[FakeReader]:
    FakeReader.requested = []
    FakeReader.behaviour = "gateway-key-from-vault-0123456789"
    monkeypatch.setattr(llm_client, "KeyVaultReader", FakeReader)
    return FakeReader


def test_development_key_override_is_registered_for_redaction() -> None:
    key = "dev-only-gateway-key-abcdef0123456789"
    assert llm_client.resolve_agent_key(_runtime(gateway_key=key)) == key
    assert default_redactor().redact(f"key={key}") == f"key={REDACTED}"


def test_key_is_read_from_the_policy_key_vault_secret(fake_reader: type[FakeReader]) -> None:
    assert llm_client.resolve_agent_key(_runtime()) == "gateway-key-from-vault-0123456789"
    assert fake_reader.requested == ["citadel-agent-llm-key"]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ResourceNotFoundError(message="missing"), "citadel-agent bootstrap"),
        (KeyVaultAccessError("Key Vault blocked the request (network rules)"), "network rules"),
    ],
)
def test_key_vault_problems_become_configuration_errors(
    fake_reader: type[FakeReader], error: Exception, message: str
) -> None:
    fake_reader.behaviour = error
    with pytest.raises(llm_client.LlmConfigurationError, match=message):
        llm_client.resolve_agent_key(_runtime())


def test_missing_agent_section_is_a_configuration_error() -> None:
    with pytest.raises(llm_client.LlmConfigurationError, match="bootstrap"):
        llm_client.resolve_agent_key(_runtime(agent=False))


def test_chat_client_targets_the_unified_ai_surface_with_the_api_key_header(
    fake_reader: type[FakeReader],
) -> None:
    client = llm_client.create_chat_client(_runtime())
    assert client.model == "gpt-5.2"
    assert str(client.client.base_url) == "https://apim-dev.azure-api.net/unified-ai/v1/"
    assert client.default_headers["api-key"] == "gateway-key-from-vault-0123456789"
    assert client.default_headers[llm_client.APP_ID_HEADER] == llm_client.APP_ID
    assert client.client.api_key == llm_client.SDK_PLACEHOLDER_KEY


# Discovery --------------------------------------------------------------------------------------


class FakeArm:
    def __init__(self, resources: list[dict[str, Any]]) -> None:
        self.resources = resources

    def list_resources(self, subscription_id: str, resource_group: str) -> list[dict[str, Any]]:
        return self.resources

    def get(self, resource_id: str, api_version: str) -> dict[str, Any]:
        if "/service/" in resource_id:
            return {
                "location": "Sweden Central",
                "properties": {"gatewayUrl": "https://apim-x.azure-api.net"},
            }
        if "userAssignedIdentities" in resource_id:
            return {"properties": {"clientId": "client-1"}}
        if "workspaces" in resource_id:
            return {"properties": {"customerId": "ws-customer-1"}}
        raise AssertionError(resource_id)


def _resource(resource_type: str, name: str) -> dict[str, str]:
    namespace, kind = resource_type.split("/", 1)
    return {
        "id": f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/{namespace}/{kind}/{name}",
        "type": resource_type,
        "name": name,
    }


HUB = [
    _resource("Microsoft.ApiManagement/service", "apim-x"),
    _resource("Microsoft.ManagedIdentity/userAssignedIdentities", "id-other"),
    _resource("Microsoft.ManagedIdentity/userAssignedIdentities", "id-apim-x"),
    _resource("Microsoft.KeyVault/vaults", "kv-x"),
    _resource("Microsoft.Insights/components", "appi-func-x"),
    _resource("Microsoft.Insights/components", "appi-apim-x"),
    _resource("Microsoft.OperationalInsights/workspaces", "log-x"),
    _resource("Microsoft.DocumentDB/databaseAccounts", "cosmos-x"),
]


def test_discovery_builds_an_environment_entry() -> None:
    environment = discover_environment(FakeArm(HUB), SUB, RG, tenant_id="tenant-1")
    assert environment["mode"] == "direct" and environment["location"] == "swedencentral"
    assert environment["gatewayUrl"] == "https://apim-x.azure-api.net"
    assert environment["apimManagedIdentity"]["name"] == "id-apim-x"
    assert environment["apimManagedIdentity"]["clientId"] == "client-1"
    assert environment["appInsights"]["name"] == "appi-apim-x"
    assert environment["keyVault"]["name"] == "kv-x"
    assert environment["logAnalyticsWorkspaceId"] == "ws-customer-1"
    assert environment["cosmos"] == {"accountName": "cosmos-x"}
    assert environment["tenantId"] == "tenant-1"


def test_discovery_forces_pr_mode_for_production() -> None:
    environment = discover_environment(FakeArm(HUB), SUB, RG, environment_class="prod", mode="direct")
    assert environment["mode"] == "pr"


def test_discovery_requires_an_api_management_instance() -> None:
    with pytest.raises(DiscoveryError, match="No API Management"):
        discover_environment(FakeArm(HUB[1:]), SUB, RG)
