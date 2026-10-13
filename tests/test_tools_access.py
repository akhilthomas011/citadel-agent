# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for code generation, discovery tools and access contract tools (fakes, no Azure)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from citadel_agent.codegen import FRAMEWORKS, CodegenError, CodeRequest, generate, model_kind
from citadel_agent.contracts import parse_intent
from citadel_agent.gateway.models import BackendContractView
from citadel_agent.tools import access, discovery
from citadel_agent.tools.registry import ToolInputError
from tool_fakes import CALLER, GATEWAY, FakeContext, make_intent, make_record

# Code generation -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("gpt-5.2", "chat"),
        ("text-embedding-3-large", "embeddings"),
        ("FLUX.2-pro", "images"),
        ("gpt-image-1.5", "images"),
    ],
)
def test_model_kind(name: str, kind: str) -> None:
    assert model_kind(name) == kind


@pytest.mark.parametrize("framework", FRAMEWORKS)
@pytest.mark.parametrize("model", ["gpt-5.2", "text-embedding-3-large", "gpt-image-1.5"])
@pytest.mark.parametrize("jwt", [None, "api://app/.default"])
def test_generated_code(framework: str, model: str, jwt: str | None) -> None:
    request = CodeRequest(
        framework, GATEWAY, model, key_secret="hr-chat-key", key_vault="kv-1", jwt_scope=jwt
    )
    try:
        code = generate(request)
    except CodegenError:
        assert (framework in {"agent-framework", "openai-javascript"} and model_kind(model) != "chat") or (
            framework == "langchain" and model_kind(model) == "images"
        )
        return
    assert "hr-chat-key" in code.code or framework == "openai-javascript"
    if framework == "azure-openai-python" and not jwt:
        assert "api_key=api_key" in code.code  # the Azure OpenAI SDK sends the api-key header itself
    else:
        assert "api-key" in code.code
    if framework != "azure-openai-python":
        assert "/unified-ai/v1" in code.code
    if jwt and framework != "openai-javascript":
        assert jwt in code.code
    if code.language == "python":
        compile(code.code, f"{framework}.py", "exec")


def test_unknown_framework() -> None:
    with pytest.raises(CodegenError, match="Unknown framework"):
        generate(CodeRequest("cobol", GATEWAY, "gpt-5.2", key_secret="k"))


CONTRACT = BackendContractView(
    {
        "backends": [
            {
                "backendId": "aif-0",
                "backendType": "ai-foundry",
                "supportedModels": [
                    {
                        "name": "gpt-5.2",
                        "capacity": 100,
                        "modelVersion": "2025-12-11",
                        "retirementDate": "2027-02-05",
                    },
                    {"name": "text-embedding-3-large", "capacity": 50},
                ],
            },
            {
                "backendId": "aif-1",
                "backendType": "ai-foundry",
                "supportedModels": [{"name": "gpt-5.2", "capacity": 100}],
            },
        ],
        "modelAliases": [{"name": "chat-default", "description": "Default chat model"}],
    }
)


@pytest.fixture
def fake_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery, "GatewayClient", lambda url: SimpleNamespace(backend_contract=lambda: CONTRACT)
    )


# Discovery tools -------------------------------------------------------------------------------


@pytest.mark.usefixtures("fake_gateway")
def test_list_models(tmp_path: Path) -> None:
    context = FakeContext(tmp_path, [make_record(make_intent(allowedModels=["gpt-5.2", "gpt-9"]))])
    everything = discovery.list_models(context)
    assert (
        "| gpt-5.2 | chat | 2025-12-11 | aif-0 (ai-foundry), aif-1 (ai-foundry) | 200 | 2027-02-05 |"
        in everything
    )
    assert "text-embedding-3-large | embeddings" in everything and "chat-default | alias" in everything

    scoped = discovery.list_models(context, business_unit="hr", use_case="chat")
    assert "gpt-5.2" in scoped and "text-embedding-3-large" not in scoped
    assert "not onboarded on the gateway: gpt-9" in scoped
    with pytest.raises(ToolInputError, match="No access contract"):
        discovery.list_models(context, business_unit="HR", use_case="Missing")


def test_generate_client_code_uses_the_contract(tmp_path: Path) -> None:
    context = FakeContext(tmp_path, [make_record(make_intent(jwt={"required": True}))])
    output = discovery.generate_client_code(context, "openai-python", "gpt-4o", "HR", "Chat")
    assert 'get_secret("hr-chat-key")' in output and "https://kv-1.vault.azure.net" in output
    assert "gpt-4o is not allowed by this contract" in output
    assert "requires a JWT" in output
    generic = discovery.generate_client_code(context, "curl", "gpt-5.2")
    assert "<key-secret-name>" in generic
    with pytest.raises(ToolInputError, match="chat models"):
        discovery.generate_client_code(context, "agent-framework", "text-embedding-3-large")


# Access contract tools -------------------------------------------------------------------------


def test_list_and_describe_access_contracts(tmp_path: Path) -> None:
    mine = make_intent()
    other = make_intent(uc="Other", owners=["someone@contoso.com"])
    consumer = FakeContext(tmp_path, [make_record(mine), make_record(other)])
    listing = access.list_access_contracts(consumer)
    assert "HR/Chat" in listing and "HR/Other" not in listing and "1 other contracts" in listing
    operator = FakeContext(tmp_path, [make_record(mine), make_record(other)], persona="operator")
    assert "HR/Other" in access.list_access_contracts(operator)

    text = access.describe_access_contract(consumer, "hr", "CHAT")
    assert "Product `LLM-HR-Chat-DEV`" in text and "secret `hr-chat-key` in vault `kv-1`" in text
    assert "5,000 tokens per minute" in text and "contract.yaml" in text


def test_draft_new_contract(tmp_path: Path) -> None:
    context = FakeContext(tmp_path, [])
    with pytest.raises(ToolInputError, match="tokens_per_minute"):
        access.draft_access_contract(context, "Sales", "Bot", models=["gpt-5.2"])
    with pytest.raises(ToolInputError, match="which models"):
        access.draft_access_contract(context, "Sales", "Bot", tokens_per_minute=1000)
    output = access.draft_access_contract(
        context,
        "Sales",
        "Bot",
        models=["gpt-5.2"],
        tokens_per_minute=1000,
        monthly_token_quota=50000,
        pii="block",
    )
    assert "New contract drafted" in output and "quota 50,000 tokens per month" in output
    draft = context.drafts.list()[0]
    service = draft.intent.services[0]
    assert service.key_secret_name == "SALES-BOT-LLM-DEV-KEY"
    assert service.apis == ["unified-ai-api", "universal-llm-api"]
    assert service.policy.pii.mode == "block" and draft.intent.metadata.owners == [CALLER]


def test_draft_refuses_secret_names_used_by_another_contract(tmp_path: Path) -> None:
    other = parse_intent(
        {
            "kind": "access-contract",
            "environment": "dev",
            "metadata": {"owners": ["someone@contoso.com"]},
            "useCase": {"businessUnit": "Sales", "useCaseName": "Legacy", "environment": "DEV"},
            "services": [
                {"code": "LLM", "apis": ["unified-ai-api"], "keySecretName": "SALES-BOT-LLM-DEV-KEY"}
            ],
        }
    )
    context = FakeContext(tmp_path, [make_record(other)])
    with pytest.raises(ToolInputError, match="sales-bot-llm-dev-key"):
        access.draft_access_contract(context, "Sales", "Bot", models=["gpt-5.2"], tokens_per_minute=1000)
    # The same use case in another environment gets its own secret names.
    access.draft_access_contract(context, "Sales", "Bot", "test", models=["gpt-5.2"], tokens_per_minute=1000)
    assert context.drafts.list()[0].intent.services[0].key_secret_name == "SALES-BOT-LLM-TEST-KEY"


def test_draft_change_keeps_existing_settings(tmp_path: Path) -> None:
    context = FakeContext(tmp_path, [make_record(make_intent(contentSafety={"enabled": True}))])
    output = access.draft_access_contract(context, "HR", "Chat", tokens_per_minute=8000)
    assert "Change to the existing contract" in output
    service = context.drafts.list()[0].intent.services[0]
    assert service.policy.token_limit.tokens_per_minute == 8000
    assert service.policy.allowed_models == ["gpt-5.2"] and service.policy.content_safety.enabled


def test_draft_rejections(tmp_path: Path) -> None:
    capped = FakeContext(
        tmp_path, [make_record(make_intent())], caps=["tokensPerMinute 90000 exceeds the cap of 50000"]
    )
    with pytest.raises(ToolInputError, match="does not allow"):
        access.draft_access_contract(capped, "HR", "Chat", tokens_per_minute=90000)
    context = FakeContext(tmp_path, [make_record(make_intent(owners=["someone@contoso.com"]))])
    with pytest.raises(PermissionError, match="owner"):
        access.draft_access_contract(context, "HR", "Chat", tokens_per_minute=100)
    with pytest.raises(ToolInputError, match="not valid"):
        access.draft_access_contract(context, "H R", "Bot", models=["gpt-5.2"], tokens_per_minute=100)
    with pytest.raises(ToolInputError, match="Foundry"):
        access.draft_access_contract(
            context, "Ops", "Bot", models=["gpt-5.2"], tokens_per_minute=100, foundry_account="aif"
        )
