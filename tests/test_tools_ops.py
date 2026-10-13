# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for diagnostic and operations tools (fakes; no Azure or gateway calls)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from citadel_agent.gateway.models import BackendContractView, GatewayResponseInfo, SmokeTestResult
from citadel_agent.tools import diagnostics, operations
from citadel_agent.tools.registry import ToolInputError
from tool_fakes import FakeContext, make_intent, make_record

REQUEST_ID = "1e0ad240-d1c2-4ce4-b68b-a53ce28a9b89"


@pytest.fixture
def context(tmp_path: Path) -> FakeContext:
    return FakeContext(tmp_path, [make_record(make_intent())])


def test_explain_error_from_the_catalog(context: FakeContext) -> None:
    text = diagnostics.explain_error(
        context, 403, '{"error": {"code": "unauthorized_model_access", "message": "Access to model x"}}'
    )
    assert "The model is not allowed for this product" in text and "Who can fix it: consumer" in text
    assert "No known Citadel error" in diagnostics.explain_error(context, 418, "teapot")


def test_explain_error_uses_the_gateway_log(context: FakeContext, monkeypatch: pytest.MonkeyPatch) -> None:
    row = {
        "CorrelationId": REQUEST_ID,
        "ResponseCode": 500,
        "ProductId": "LLM-HR-Chat-DEV",
        "Url": "https://gw/unified-ai/v1/models?x=1",
        "LastErrorSource": "request-forwarder",
        "LastErrorMessage": "Backend service URL is not defined.",
    }
    monkeypatch.setattr(diagnostics, "_query_logs", lambda *args: [row])
    text = diagnostics.explain_error(
        context, None, '{"message": "Internal server error"}', request_id=REQUEST_ID
    )
    assert "No backend was selected" in text and "Gateway log for this request" in text
    assert "?x=1" not in text  # query strings are dropped from logged URLs


def test_gateway_logs_are_scoped_to_owned_products(
    context: FakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    queries: list[str] = []

    def fake_query(ctx: Any, env: str, kql: str, minutes: int) -> list[dict[str, Any]]:
        queries.append(kql)
        return []

    monkeypatch.setattr(diagnostics, "_query_logs", fake_query)
    assert "No matching gateway log entries" in diagnostics.gateway_logs(context)
    assert "ProductId in ('LLM-HR-Chat-DEV')" in queries[0] and "ResponseCode >= 400" in queries[0]
    with pytest.raises(ToolInputError, match="request ID looks like"):
        diagnostics.gateway_logs(context, request_id="x' or 1==1")
    stranger = FakeContext(
        context.drafts.directory.parent, [make_record(make_intent(owners=["x@contoso.com"]))]
    )
    assert "do not own any access contract" in diagnostics.gateway_logs(stranger)


class FakeGateway:
    def __init__(self, url: str) -> None:
        self.calls: list[tuple[str, str]] = []

    def chat_smoke_test(self, key: str, model: str) -> SmokeTestResult:
        assert key == "contract-key-0123456789abcdef"
        info = GatewayResponseInfo(
            status_code=200, latency_ms=120.0, region="Sweden Central", remaining_tokens="4980"
        )
        return SmokeTestResult("chat.completions", model, info, usage={"total_tokens": 20}, preview="OK")

    def close(self) -> None:
        return None


def test_smoke_test(context: FakeContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diagnostics,
        "KeyVaultReader",
        lambda credential, url: SimpleNamespace(
            get_secret_in_memory=lambda name: "contract-key-0123456789abcdef"
        ),
    )
    monkeypatch.setattr(diagnostics, "GatewayClient", FakeGateway)
    text = diagnostics.smoke_test(context, "HR", "Chat")
    assert "passed" in text and "Remaining tokens this minute: 4980" in text and "Model replied: OK" in text
    assert "contract-key" not in text

    others = FakeContext(
        context.drafts.directory.parent, [make_record(make_intent(owners=["x@contoso.com"]))]
    )
    with pytest.raises(PermissionError):
        diagnostics.smoke_test(others, "HR", "Chat")
    jwt = FakeContext(context.drafts.directory.parent, [make_record(make_intent(jwt={"required": True}))])
    with pytest.raises(ToolInputError, match="JWT"):
        diagnostics.smoke_test(jwt, "HR", "Chat")


def test_cost_uses_cached_and_output_prices() -> None:
    row = {"prompt": 1000, "cached": 200, "completion": 100}
    price = {
        "CostPerInputUnit": 2,
        "CostPerCachedInputUnit": 0.5,
        "CostPerOutputUnit": 8,
        "CostUnit": 1_000_000,
    }
    assert operations._cost(row, price) == pytest.approx(0.0025)
    assert operations._cost(row, {**price, "CalculationMethod": "percentage"}) is None
    assert operations._cost(row, None) is None


USAGE = [
    {
        "product": "LLM HR Chat DEV",
        "model": "gpt-5.2",
        "app": "a1",
        "day": "2026-10-07",
        "prompt": 1000,
        "cached": 0,
        "completion": 500,
    },
    {
        "product": "LLM HR Chat DEV",
        "model": "gpt-5.2",
        "app": "a2",
        "day": "2026-10-07",
        "prompt": 500,
        "cached": 0,
        "completion": 0,
    },
]


def test_usage_report_from_cosmos_with_costs(context: FakeContext, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def fake_cosmos(
        ctx: Any, env: Any, products: Any, days: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        seen.append(products)
        return list(USAGE), {"gpt-5.2": {"CostPerInputUnit": 2, "CostPerOutputUnit": 8, "Currency": "USD"}}

    monkeypatch.setattr(operations, "_cosmos_usage", fake_cosmos)
    text = operations.usage_report(context, group_by="app")
    assert seen == [["LLM HR Chat DEV"]]  # consumers only see their own products
    assert "| a1 | 1,000 | 0 | 500 | 1,500 | 0.0060 |" in text
    assert "| Total | 1,500 | 0 | 500 | 2,000 | 0.0070 |" in text and "Source: Cosmos DB" in text


def test_usage_report_falls_back_to_metrics(context: FakeContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any) -> Any:
        raise RuntimeError("Request originated from IP 1.2.3.4 is blocked by your Cosmos DB account firewall")

    monkeypatch.setattr(operations, "_cosmos_usage", broken)
    monkeypatch.setattr(operations, "_metric_usage", lambda *args: list(USAGE))
    text = operations.usage_report(context)
    assert "Application Insights token metrics (usage only, no cost)" in text
    assert "Estimated cost" not in text and "Cosmos DB was not available" in text


def test_backend_contract_summary_hides_endpoints_from_consumers(
    context: FakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = BackendContractView(
        {
            "backends": [
                {
                    "backendId": "b1",
                    "backendType": "ai-foundry",
                    "endpoint": "https://b1.example/",
                    "supportedModels": ["gpt-5.2"],
                }
            ]
        }
    )
    monkeypatch.setattr(
        operations, "GatewayClient", lambda url: SimpleNamespace(backend_contract=lambda: contract)
    )
    assert "https://b1.example/" not in operations.backend_contract_summary(context)
    operator = FakeContext(context.drafts.directory.parent, [], persona="operator")
    assert "https://b1.example/" in operations.backend_contract_summary(operator)
