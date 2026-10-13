# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the gateway data-plane client (HTTP mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from citadel_agent.gateway import BackendContractView, GatewayClient, GatewayRequestError, parse_error
from citadel_agent.safety.redaction import REDACTED, Redactor

GW = "https://apim-test.azure-api.net"
KEY = "0123456789abcdef0123456789abcdef"

BACKEND_CONTRACT = {
    "masterVersion": "1.0.2",
    "backendContractVersion": "1.1.0",
    "circuitBreaker": {"enabled": True, "defaults": {"failureCount": 3}},
    "sessionAffinity": {"enabled": True, "defaults": {"cookieName": "ai-gateway-affinity"}},
    "modelAliases": [{"name": "smart", "models": ["gpt-5.2", "gpt-5.4-mini"], "strategy": "priority"}],
    "backends": [
        {
            "backendId": "aif-0",
            "backendType": "ai-foundry",
            "supportedModels": [{"name": "gpt-5.2"}, {"name": "Mistral-Large-3"}],
        },
        {"backendId": "aif-1", "backendType": "ai-foundry", "supportedModels": [{"name": "gpt-5.2"}]},
        {
            "backendId": "gemini",
            "backendType": "gemini-openai",
            "authConfig": {"namedValueKey": "gemini-key", "secretValue": "***redacted***"},
            "supportedModels": ["gemini-2.5-flash"],
        },
    ],
    "pools": [{"poolName": "gpt-5.2-backend-pool", "poolType": "ai-foundry", "supportedModels": ["gpt-5.2"]}],
}


@respx.mock
def test_version_is_anonymous() -> None:
    route = respx.get(f"{GW}/version").mock(
        return_value=httpx.Response(200, json={"master-version": "1.0.2"})
    )
    assert GatewayClient(GW).version() == {"master-version": "1.0.2"}
    assert "api-key" not in route.calls[0].request.headers


@respx.mock
def test_backend_contract_view() -> None:
    respx.get(f"{GW}/version/backend-contract").mock(return_value=httpx.Response(200, json=BACKEND_CONTRACT))
    view = GatewayClient(GW).backend_contract()
    assert view.backend_contract_version == "1.1.0"
    assert view.has_model("GPT-5.2") and view.has_model("smart") and view.has_model("gemini-2.5-flash")
    assert view.backends_for_model("gpt-5.2") == ["aif-0", "aif-1"]
    assert view.circuit_breaker_enabled
    assert view.has_redacted_secrets()


def test_backend_contract_without_secrets() -> None:
    clean = dict(BACKEND_CONTRACT, backends=BACKEND_CONTRACT["backends"][:2])
    assert not BackendContractView(clean).has_redacted_secrets()


@respx.mock
def test_list_models_sends_key_and_parses_aliases() -> None:
    document = {
        "value": [
            {
                "id": "aif-0",
                "type": "ai-foundry",
                "name": "gpt-5.2",
                "sku": {"name": "GlobalStandard", "capacity": 100},
                "properties": {"model": {"format": "OpenAI", "name": "gpt-5.2", "version": "2025-12-11"}},
            },
            {"name": "smart", "type": "alias", "properties": {"model": {"format": "Alias", "name": "smart"}}},
        ]
    }
    route = respx.get(f"{GW}/unified-ai/deployments").mock(return_value=httpx.Response(200, json=document))
    redactor = Redactor()
    models = GatewayClient(GW, redactor=redactor).list_models(KEY)

    assert [(m.name, m.kind) for m in models] == [("gpt-5.2", "model"), ("smart", "alias")]
    assert models[0].capacity == 100
    assert route.calls[0].request.headers["api-key"] == KEY
    assert redactor.redact(KEY) == REDACTED


@respx.mock
def test_chat_smoke_test_success_reports_diagnostics() -> None:
    respx.post(f"{GW}/unified-ai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}], "usage": {"total_tokens": 12}},
            headers={"x-ms-request-id": "req-1", "x-ms-region": "Sweden Central", "UAIG-Backend": "aif-0"},
        )
    )
    result = GatewayClient(GW).chat_smoke_test(KEY, "gpt-5.2")
    assert result.ok
    assert result.preview == "OK"
    assert result.usage == {"total_tokens": 12}
    assert result.info.request_id == "req-1"
    assert result.info.debug_headers == {"uaig-backend": "aif-0"}


@respx.mock
def test_chat_smoke_test_failure_parses_citadel_error() -> None:
    respx.post(f"{GW}/unified-ai/v1/chat/completions").mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "message": "Model not allowed",
                    "type": "access_error",
                    "code": "unauthorized_model_access",
                    "allowed_models": ["gpt-5.2"],
                }
            },
        )
    )
    result = GatewayClient(GW).chat_smoke_test(KEY, "gpt-4o")
    assert not result.ok
    assert result.info.error is not None
    assert result.info.error.code == "unauthorized_model_access"


@respx.mock
def test_required_call_failure_raises_with_status() -> None:
    respx.get(f"{GW}/version").mock(return_value=httpx.Response(404, text="Resource not found"))
    with pytest.raises(GatewayRequestError) as excinfo:
        GatewayClient(GW).version()
    assert "404" in str(excinfo.value)


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        (
            {"error": {"code": "jwt_required", "message": "JWT bearer token is required"}},
            "jwt_required",
            "JWT bearer token is required",
        ),
        (
            {"statusCode": 401, "message": "Access denied due to missing subscription key."},
            None,
            "Access denied due to missing subscription key.",
        ),
        ({"error": "PII_DETECTED"}, None, "PII_DETECTED"),
        ("plain text failure", None, "plain text failure"),
    ],
)
def test_parse_error_shapes(body: object, code: str | None, message: str) -> None:
    error = parse_error(400, body)
    assert error.code == code
    assert error.message == message
