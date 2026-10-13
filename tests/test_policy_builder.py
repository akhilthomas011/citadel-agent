# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the product policy builder and the APIM policy linter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from defusedxml import ElementTree

from citadel_agent.contracts import AccessContractIntent, ServiceIntent
from citadel_agent.contracts.policy_builder import (
    GENERATED_HEADER,
    JWT_TENANT_NAMED_VALUE,
    PolicyBuildContext,
    PolicyBuildError,
    build_product_policy,
)
from citadel_agent.contracts.policy_xml import LintLevel, has_errors, lint_policy, mask_expressions

UPSTREAM = Path(__file__).resolve().parents[1] / "upstream" / "ai-hub-gateway" / "bicep" / "infra"
TENANT = "22222222-2222-2222-2222-222222222222"


def service(policy: dict[str, Any] | None = None, **overrides: Any) -> ServiceIntent:
    data: dict[str, Any] = {
        "code": "LLM",
        "apis": ["universal-llm-api", "azure-openai-api", "unified-ai-api"],
        "keySecretName": "HR-LLM-KEY",
        "policy": policy or {},
    }
    data.update(overrides)
    return ServiceIntent.model_validate(data)


def parse(text: str) -> Any:
    masked, _ = mask_expressions(text)
    return ElementTree.fromstring(masked)


def variables(text: str) -> dict[str, str]:
    return {e.get("name"): e.get("value") for e in parse(text).iter("set-variable")}


# Linter ----------------------------------------------------------------------------------------


def _upstream_policy_files() -> list[Path]:
    return sorted(UPSTREAM.rglob("*.xml")) if UPSTREAM.is_dir() else []


@pytest.mark.parametrize("path", _upstream_policy_files(), ids=lambda p: p.name)
def test_expression_masker_handles_every_upstream_policy(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if "//{" in text:  # Bicep-substituted template placeholders are not valid policies on their own
        pytest.skip("template with Bicep placeholders")
    masked, expressions = mask_expressions(text)
    ElementTree.fromstring(masked)
    assert all(expr.startswith(("@(", "@{")) for expr in expressions)


def test_upstream_default_product_policies_lint_clean() -> None:
    for name in ("default-ai-product-policy.xml", "default-multi-product-policy.xml"):
        text = (UPSTREAM / "citadel-access-contracts" / "policies" / name).read_text(encoding="utf-8")
        assert not has_errors(lint_policy(text)), name


def test_lint_reports_structural_problems() -> None:
    assert has_errors(lint_policy("<policies><inbound><base /></inbound><inbound /></policies>"))
    assert has_errors(lint_policy("<fragment />"))
    assert has_errors(
        lint_policy('<policies><inbound><set-variable name="x" value="@(1 + " /></inbound></policies>')
    )
    spaced = (
        '<policies><inbound><base /><set-variable name="allowedModels" value="a, b" /></inbound></policies>'
    )
    assert has_errors(lint_policy(spaced))
    warnings = lint_policy("<policies><inbound /></policies>")
    assert warnings and warnings[0].level is LintLevel.WARNING


def test_masker_ignores_braces_inside_verbatim_strings() -> None:
    text = '<x value="@{ var p = @"\\d{4}"; return p; }" />'
    masked, expressions = mask_expressions(text)
    assert masked == '<x value="__APIM_EXPR_0__" />'
    assert expressions == ['@{ var p = @"\\d{4}"; return p; }']


# LLM product policies --------------------------------------------------------------------------


def test_minimal_llm_policy_matches_upstream_shape() -> None:
    text = build_product_policy(service({"allowedModels": ["gpt-5.2", "text-embedding-3-large"]}))
    assert text.startswith(GENERATED_HEADER)
    assert not has_errors(lint_policy(text))
    root = parse(text)
    inbound = root.find("inbound")
    assert inbound is not None and inbound[0].tag == "base"
    assert variables(text)["allowedModels"] == "gpt-5.2,text-embedding-3-large"
    assert '<include-fragment fragment-id="set-llm-requested-model" />' in text
    assert "validate-model-access" not in text  # done by the API-level policy for LLM products


def test_token_limits_single_and_per_model() -> None:
    single = build_product_policy(
        service(
            {"tokenLimit": {"tokensPerMinute": 5000, "tokenQuota": 500000, "tokenQuotaPeriod": "Monthly"}}
        )
    )
    limit = parse(single).find("inbound/llm-token-limit")
    assert limit is not None
    assert limit.get("tokens-per-minute") == "5000" and limit.get("token-quota-period") == "Monthly"

    per_model = build_product_policy(
        service(
            {
                "allowedModels": ["gpt-5.2", "GPT-5.4-mini"],
                "tokenLimit": {"tokensPerMinute": 1000},
                "modelTokenLimits": [{"model": "GPT-5.4-mini", "tokensPerMinute": 20000}],
            }
        )
    )
    assert not has_errors(lint_policy(per_model))
    assert '.ToLowerInvariant() == "gpt-5.4-mini"' in per_model
    assert '@(context.Subscription.Id + "-gpt-5.4-mini")' in per_model
    assert '@(context.Subscription.Id + "-default")' in per_model


def test_jwt_roles_alerts_dimensions_and_debug_headers() -> None:
    text = build_product_policy(
        service(
            {
                "jwt": {"required": True, "requiredRoles": ["Models.Read", "Agent.Read"]},
                "alerts": {"authFailure": True, "throttling": False},
                "customDimension1": {"header": "x-enduser-id", "default": "anonymous"},
                "customDimension2": {"staticValue": "cost-center-4711"},
                "responseHeaders": True,
            }
        )
    )
    values = variables(text)
    assert values["jwtRequired"] == "true"
    assert values["requiredRoles"] == "Models.Read,Agent.Read"
    assert values["alertOnAuthFailure"] == "true" and values["alertOnThrottling"] == "false"
    assert "alertOnQuotaExceeded" not in values  # defaults are not repeated
    assert values["customDimension2"] == "cost-center-4711"
    assert 'value="@(context.Request.Headers.GetValueOrDefault("x-enduser-id", "anonymous"))"' in text
    issues = lint_policy(text)
    assert any("enableResponseHeaders" in issue.message for issue in issues)


def test_content_safety_block() -> None:
    text = build_product_policy(
        service({"contentSafety": {"enabled": True, "thresholds": {"Hate": 2, "Violence": 4}}})
    )
    safety = parse(text).find("inbound/llm-content-safety")
    assert safety is not None and safety.get("backend-id") == "content-safety-backend"
    categories = {c.get("name"): c.get("threshold") for c in safety.iter("category")}
    assert categories == {"Hate": "2", "Violence": "4"}


def test_pii_anonymize_and_block_modes() -> None:
    anonymize = build_product_policy(
        service(
            {
                "pii": {
                    "mode": "anonymize",
                    "regexPatterns": [{"pattern": r"\b\d{4}[- ]?\d{4}\b", "category": "CARD"}],
                    "saveState": True,
                }
            }
        )
    )
    assert not has_errors(lint_policy(anonymize))
    assert '<include-fragment fragment-id="pii-anonymization" />' in anonymize
    assert '<include-fragment fragment-id="pii-deanonymization" />' in anonymize
    assert '<include-fragment fragment-id="pii-state-saving" />' in anonymize
    assert '@"\\b\\d{4}[- ]?\\d{4}\\b"' in anonymize

    block = build_product_policy(service({"pii": {"mode": "block"}}))
    assert not has_errors(lint_policy(block))
    assert variables(block)["piiBlockingEnabled"] == "true"
    assert "PII_DETECTED" in block
    assert "pii-deanonymization" not in block


# Foundry managed identity ----------------------------------------------------------------------


def test_foundry_managed_identity_adds_jwt_override_with_literal_or_named_tenant() -> None:
    literal = variables(
        build_product_policy(service(), PolicyBuildContext(foundry_managed_identity=True, tenant_id=TENANT))
    )
    assert literal["jwtRequired"] == "true"
    assert literal["jwtAudience"] == "https://cognitiveservices.azure.com"
    assert literal["jwtIssuer"] == f"https://sts.windows.net/{TENANT}/"

    named = variables(build_product_policy(service(), PolicyBuildContext(foundry_managed_identity=True)))
    assert JWT_TENANT_NAMED_VALUE in named["jwtIssuer"]


@pytest.mark.parametrize(
    "jwt",
    [
        {"audience": "api://custom"},
        {"issuer": "https://issuer.example.com/"},
        {"required": True, "requiredRoles": ["Models.Read"]},
    ],
)
def test_foundry_managed_identity_conflicts(jwt: dict[str, Any]) -> None:
    with pytest.raises(PolicyBuildError):
        build_product_policy(service({"jwt": jwt}), PolicyBuildContext(foundry_managed_identity=True))


# Multi-asset product policies ------------------------------------------------------------------


def test_multi_asset_policy_classifies_and_guards_pii_outbound() -> None:
    multi = service(
        {
            "allowedModels": ["gpt-5.2"],
            "tokenLimit": {"tokensPerMinute": 1000},
            "toolLimits": {"calls": 60, "quotaCalls": 10000, "quotaPeriodSeconds": 2592000},
            "agentLimits": {"calls": 30},
            "pii": {"mode": "anonymize"},
        },
        code="MULTI",
        apis=["universal-llm-api", "weather-tool", "hr-chat-agent"],
        toolApis=["weather-tool"],
        agentApis=["hr-chat-agent"],
    )
    text = build_product_policy(multi)
    assert not has_errors(lint_policy(text))
    values = variables(text)
    assert values["contractToolApis"] == "weather-tool"
    assert values["contractAgentApis"] == "hr-chat-agent"
    assert '<include-fragment fragment-id="set-asset-kind" />' in text
    assert '<include-fragment fragment-id="validate-model-access" />' in text
    assert 'counter-key="@(context.Subscription.Id + ":tool")"' in text
    assert '<quota-by-key calls="10000" renewal-period="2592000"' in text
    outbound = parse(text).find("outbound")
    assert outbound is not None
    guard = outbound.find("choose/when")
    assert guard is not None and guard.find("set-variable").get("name") == "responseBodyContent"


def test_output_is_deterministic() -> None:
    intent = service({"allowedModels": ["gpt-5.2"], "contentSafety": {"enabled": True}})
    assert build_product_policy(intent) == build_product_policy(intent)


def test_non_llm_service_gets_minimal_policy() -> None:
    doc = service(code="DOC", apis=["document-intelligence-api"])
    text = build_product_policy(doc)
    assert "set-llm-requested-model" not in text
    assert not has_errors(lint_policy(text))


def test_policy_for_full_contract_intent() -> None:
    contract = AccessContractIntent.model_validate(
        {
            "environment": "dev",
            "useCase": {"businessUnit": "HR", "useCaseName": "ChatAgent", "environment": "DEV"},
            "services": [service({"allowedModels": ["gpt-5.2"]}).model_dump(by_alias=True)],
        }
    )
    assert "gpt-5.2" in build_product_policy(contract.services[0])
