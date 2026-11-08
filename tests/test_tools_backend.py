# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the backend onboarding tool (fakes; no Azure)."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from citadel_agent.changes.repository import IntentRecord
from citadel_agent.contracts.backend import BackendContractIntent, ProviderBackendDraft
from citadel_agent.gateway.models import BackendContractView
from citadel_agent.tools import backend
from citadel_agent.tools.registry import ToolInputError, ToolRegistry
from tool_fakes import FakeContext, make_intent, make_record

SUB = "11111111-1111-1111-1111-111111111111"
LIVE = {
    "circuitBreaker": {"enabled": True, "defaults": {"failureCount": 3}},
    "sessionAffinity": {"enabled": False},
    "backends": [
        {
            "backendId": "aif-0",
            "backendType": "ai-foundry",
            "endpoint": "https://aif-0.cognitiveservices.azure.com/",
            "authType": "managed-identity",
            "supportedModels": [{"name": "gpt-5.2", "sku": "GlobalStandard", "capacity": 100}],
            "priority": 1,
            "weight": 100,
        }
    ],
    "modelAliases": [],
}


class FakeArm:
    def __init__(self, assigned: bool = False) -> None:
        self.assigned = assigned

    def list_cognitive_accounts(self, subscription_id: str) -> list[dict[str, Any]]:
        provider = "providers/Microsoft.CognitiveServices/accounts"
        account_id = f"/subscriptions/{subscription_id}/resourceGroups/rg/{provider}/aif-new"
        return [
            {
                "id": account_id,
                "name": "aif-new",
                "kind": "AIServices",
                "properties": {"endpoint": "https://aif-new.example/"},
            }
        ]

    def list_cognitive_deployments(self, account_id: str) -> list[dict[str, Any]]:
        return [
            {
                "name": "gpt-5.2",
                "sku": {"name": "GlobalStandard", "capacity": 50},
                "properties": {"model": {"format": "OpenAI", "version": "1"}},
            },
            {
                "name": "o4-mini",
                "sku": {"name": "GlobalStandard", "capacity": 20},
                "properties": {"model": {"format": "OpenAI"}},
            },
        ]

    def get(self, path: str, api_version: str) -> dict[str, Any]:
        return {"properties": {"principalId": "11111111-2222-3333-4444-555555555555"}}

    def list(self, path: str, api_version: str, params: Any = None) -> list[dict[str, Any]]:
        role = "/providers/Microsoft.Authorization/roleDefinitions/a97b65f3-24c7-4388-baec-2e87135dc908"
        return [{"properties": {"roleDefinitionId": role}}] if self.assigned else []


class BackendContext(FakeContext):
    def __init__(
        self,
        tmp: Path,
        *,
        backend_intent: Any = None,
        access: list[Any] | None = None,
        assigned: bool = False,
    ):
        super().__init__(tmp, access or [], persona="operator")
        self.backend_records = (
            [IntentRecord(PurePosixPath("contracts/backend/dev/contract.yaml"), backend_intent)]
            if backend_intent
            else []
        )
        env = self.environment()
        env.subscription_id = SUB
        env.apim_managed_identity = SimpleNamespace(
            subscription_id=SUB, resource_group_name="rg", name="id-apim", client_id="c"
        )
        self.runtime.arm = FakeArm(assigned)
        self.runtime.authorizer.require = lambda operation: None

    def repository(self) -> Any:
        return SimpleNamespace(
            intents=lambda kind: self.backend_records if kind == "backend-contract" else []
        )


@pytest.fixture
def live_gateway(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state = {"raw": dict(LIVE)}
    monkeypatch.setattr(
        backend,
        "GatewayClient",
        lambda url: SimpleNamespace(backend_contract=lambda: BackendContractView(state["raw"])),
    )
    return state


def test_first_use_imports_the_live_contract(tmp_path: Path, live_gateway: dict[str, Any]) -> None:
    context = BackendContext(tmp_path)
    intent, source = backend.current_backend_intent(context, "dev")
    assert "live gateway" in source and intent.backends[0].backend_id == "aif-0"
    assert intent.circuit_breaker_enabled and not intent.session_affinity_enabled
    live_gateway["raw"] = {
        **LIVE,
        "backends": [{**LIVE["backends"][0], "authConfig": {"secretValue": "***redacted***"}}],
    }
    with pytest.raises(ToolInputError, match="inline secrets"):
        backend.current_backend_intent(context, "dev")


@pytest.mark.usefixtures("live_gateway")
def test_add_account_discovers_deployments_and_role_prerequisite(tmp_path: Path) -> None:
    context = BackendContext(tmp_path)
    output = backend.draft_backend_change(context, add_account="aif-new", models=["o4-mini"], priority=2)
    assert "Added backend aif-new: o4-mini" in output and "Prerequisite (manual)" in output
    draft = context.drafts.list()[0]
    new = next(b for b in draft.intent.backends if b.backend_id == "aif-new")
    assert new.priority == 2 and [m.name for m in new.supported_models] == ["o4-mini"]
    assert "az role assignment create" in draft.prerequisites[0]["commands"][0]

    assigned = BackendContext(tmp_path / "b", assigned=True)
    assert "Prerequisite" not in backend.draft_backend_change(assigned, add_account="aif-new")


@pytest.mark.usefixtures("live_gateway")
def test_add_provider_backend_merges_key_vault_credentials_into_authoritative_intent(tmp_path: Path) -> None:
    context = BackendContext(tmp_path)
    provider = ProviderBackendDraft.model_validate(
        {
            "backendId": "gemini-openai-prod",
            "backendType": "gemini-openai",
            "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "models": ["gemini-2.5-flash"],
            "authType": "api-key-bearer",
            "authConfig": {
                "namedValueKey": "gemini-openai-key",
                "keyVaultSecretUri": "https://kv.example.vault.azure.net/secrets/gemini-openai-key",
            },
        }
    )

    output = backend.draft_backend_change(context, provider_backend=provider, priority=2)

    assert "Added backend gemini-openai-prod: gemini-2.5-flash" in output
    draft = context.drafts.list()[0]
    assert {b.backend_id for b in draft.intent.backends} == {"aif-0", "gemini-openai-prod"}
    added = next(b for b in draft.intent.backends if b.backend_id == "gemini-openai-prod")
    assert added.backend_type == "gemini-openai"
    assert added.priority == 2
    assert added.auth_config is not None
    assert added.auth_config.named_value_key == "gemini-openai-key"
    assert (
        added.auth_config.key_vault_secret_uri
        == "https://kv.example.vault.azure.net/secrets/gemini-openai-key"
    )


def test_provider_backend_rejects_incompatible_auth_and_plaintext_credentials() -> None:
    base = {
        "backendId": "gemini",
        "backendType": "gemini",
        "endpoint": "https://generativelanguage.googleapis.com/",
        "models": ["gemini-2.5-flash"],
    }
    with pytest.raises(ValueError, match="api-key-gemini"):
        ProviderBackendDraft.model_validate({**base, "authType": "api-key-bearer"})
    with pytest.raises(ValueError, match=r"authConfig\.keyVaultSecretUri"):
        ProviderBackendDraft.model_validate({**base, "authType": "api-key-gemini"})
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ProviderBackendDraft.model_validate(
            {
                **base,
                "authType": "api-key-gemini",
                "authConfig": {
                    "namedValueKey": "gemini-key",
                    "keyVaultSecretUri": "https://kv.example.vault.azure.net/secrets/gemini-key",
                    "secretValue": "must-not-be-accepted",
                },
            }
        )
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ProviderBackendDraft.model_validate(
            {
                "backendId": "external",
                "backendType": "external",
                "endpoint": "https://provider.example.com/",
                "models": ["model-v1"],
                "authType": "api-key-header",
                "authConfig": {
                    "namedValueKey": "provider-key",
                    "keyVaultSecretUri": "https://kv.example.vault.azure.net/secrets/provider-key",
                    "headerName": "x-custom-key",
                },
            }
        )


@pytest.mark.parametrize(
    ("backend_type", "auth_type"),
    [
        ("aws-bedrock", "api-key-bearer"),
        ("aws-bedrock-mantle", "api-key-bearer"),
        ("gemini", "api-key-gemini"),
        ("gemini-openai", "api-key-bearer"),
        ("anthropic", "api-key-anthropic"),
        ("external", "none"),
        ("external", "api-key-bearer"),
        ("external", "api-key-header"),
    ],
)
def test_provider_backend_draft_accepts_supported_auth_pairs(backend_type: str, auth_type: str) -> None:
    provider: dict[str, Any] = {
        "backendId": "provider",
        "backendType": backend_type,
        "endpoint": "https://provider.example.com/",
        "models": ["model-v1"],
        "authType": auth_type,
    }
    if auth_type != "none":
        provider["authConfig"] = {
            "namedValueKey": "provider-key",
            "keyVaultSecretUri": "https://kv.example.vault.azure.net/secrets/provider-key",
        }

    result = ProviderBackendDraft.model_validate(provider)

    assert result.backend_type == backend_type
    assert result.auth_type == auth_type


def test_provider_backend_fields_are_exposed_only_to_operator_tool_schema(tmp_path: Path) -> None:
    registry = ToolRegistry()
    backend.register_backend_tools(registry, BackendContext(tmp_path))

    operator_tool = next(t for t in registry.build("operator") if t.name == "draft_backend_change")
    schema = json.dumps(operator_tool.to_json_schema_spec())

    assert "provider_backend" in schema
    assert "keyVaultSecretUri" in schema
    assert "headerName" not in schema
    assert "secretValue" not in schema
    assert "draft_backend_change" not in {t.name for t in registry.build("consumer")}


@pytest.mark.usefixtures("live_gateway")
def test_provider_backend_cannot_repurpose_an_existing_id_for_another_type(tmp_path: Path) -> None:
    context = BackendContext(tmp_path)
    provider = ProviderBackendDraft.model_validate(
        {
            "backendId": "aif-0",
            "backendType": "external",
            "endpoint": "https://llm.example.com/",
            "models": ["custom-model"],
            "authType": "none",
        }
    )
    with pytest.raises(ToolInputError, match="already belongs to provider ai-foundry"):
        backend.draft_backend_change(context, provider_backend=provider)


def test_alias_and_removal_impact(tmp_path: Path) -> None:
    intent = BackendContractIntent.model_validate({"environment": "dev", **{"backends": LIVE["backends"]}})
    contracts = [make_record(make_intent())]  # allows gpt-5.2
    context = BackendContext(tmp_path, backend_intent=intent, access=contracts)
    with pytest.raises(ToolInputError, match="Nothing to change"):
        backend.draft_backend_change(context)
    aliased = backend.draft_backend_change(context, alias_name="chat", alias_models=["gpt-5.2"])
    assert "Alias chat -> gpt-5.2" in aliased and "contracts repository" in aliased
    with pytest.raises(ToolInputError, match="not valid"):
        backend.draft_backend_change(context, remove_backend="aif-0")  # a contract needs at least one backend
    with pytest.raises(ToolInputError, match="No backend named"):
        backend.draft_backend_change(context, remove_backend="missing")


def test_removal_warns_about_contracts_that_lose_models(tmp_path: Path) -> None:
    second = {**LIVE["backends"][0], "backendId": "aif-1", "supportedModels": [{"name": "o4-mini"}]}
    intent = BackendContractIntent.model_validate(
        {"environment": "dev", "backends": [LIVE["backends"][0], second]}
    )
    context = BackendContext(tmp_path, backend_intent=intent, access=[make_record(make_intent())])
    output = backend.draft_backend_change(context, remove_backend="aif-0")
    assert "no longer be served: gpt-5.2" in output and "HR/Chat" in output
