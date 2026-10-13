# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for upstream drift detection and parameter importers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from citadel_agent.azure_clients.bicep import compile_params
from citadel_agent.contracts.importer import (
    access_intent_from_parameters,
    alerts_intent_from_parameters,
    backend_intent_from_config,
    unwrap,
)
from citadel_agent.upstream.drift import (
    DriftLevel,
    PropertyScan,
    check_property_usage,
    run_drift_checks,
    upstream_properties,
)
from citadel_agent.upstream.layout import UpstreamLayout, locate_upstream

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def layout() -> UpstreamLayout:
    if not (REPO_ROOT / "upstream" / "ai-hub-gateway" / "release.json").is_file():
        pytest.skip("Upstream submodule is not checked out")
    return locate_upstream(REPO_ROOT)


# Tokenizer and property extraction -------------------------------------------------------------


def test_property_extraction_handles_urls_interpolation_and_contains() -> None:
    text = (
        "var a = 'https://example.com/x' // comment with s.notAProperty\n"
        "var b = kvName(s.apiKeySecretName)\n"
        "var c = 'deploy-${namePrefix}-${alert.name}'\n"
        "var d = contains(s, 'endpointSecretName') ? s.?foundryApiName : ''\n"
        "/* block s.alsoNot */ var e = item.s.code\n"
        "var f = '''multi s.ignored'''\n"
    )
    assert upstream_properties(text, ["s", "item.s"]) == {
        "apiKeySecretName",
        "endpointSecretName",
        "foundryApiName",
        "code",
    }
    assert upstream_properties(text, ["alert"]) == {"name"}


def test_property_drift_reports_unread_and_unsupported_keys(tmp_path: Path) -> None:
    infra = tmp_path / "bicep" / "infra" / "mod"
    infra.mkdir(parents=True)
    (infra / "main.bicep").write_text("var x = alert.name\nvar y = alert.newSetting\n", encoding="utf-8")
    scan = PropertyScan(
        name="alerts",
        files=("mod/main.bicep",),
        variables=("alert",),
        rendered_keys=frozenset({"name", "gone"}),
    )
    import citadel_agent.upstream.drift as drift

    original = drift.PROPERTY_SCANS
    drift.PROPERTY_SCANS = (scan,)
    try:
        findings = check_property_usage(UpstreamLayout(tmp_path))
    finally:
        drift.PROPERTY_SCANS = original
    levels = {(f.level, f.message.split(":")[0]) for f in findings}
    assert (DriftLevel.ERROR, "agent renders keys upstream never reads") in levels
    assert (DriftLevel.WARNING, "upstream reads keys the agent cannot set") in levels


# Drift against the pinned upstream -------------------------------------------------------------


def test_no_property_drift_against_pinned_upstream(layout: UpstreamLayout) -> None:
    assert run_drift_checks(layout, compile_templates=False) == []


@pytest.mark.az
def test_no_parameter_drift_against_pinned_upstream(layout: UpstreamLayout) -> None:
    assert run_drift_checks(layout, compile_templates=True) == []


# Importers -------------------------------------------------------------------------------------


def test_unwrap_arm_parameters() -> None:
    assert unwrap({"parameters": {"a": {"value": 1}, "b": {"value": [2]}}}) == {"a": 1, "b": [2]}


LIVE_BACKEND_CONFIG = [
    {
        "backendId": "aif-abcdefghij123-0",
        "backendType": "ai-foundry",
        "endpoint": "https://aif-abcdefghij123-0.cognitiveservices.azure.com/",
        "authType": "managed-identity",
        "supportedModels": [
            {"name": "gpt-5.2", "sku": "GlobalStandard", "capacity": 100, "modelFormat": "OpenAI"},
            {
                "name": "FLUX.2-pro",
                "sku": "GlobalStandard",
                "capacity": 1,
                "modelFormat": "Black Forest Labs",
            },
        ],
        "priority": 1,
        "weight": 100,
    },
    {
        "backendId": "aif-abcdefghij123-1",
        "backendType": "ai-foundry",
        "endpoint": "https://aif-abcdefghij123-1.cognitiveservices.azure.com/",
        "authType": "managed-identity",
        "supportedModels": [{"name": "gpt-5.2"}],
        "priority": 1,
        "weight": 100,
    },
]


def test_backend_intent_from_hub_output() -> None:
    intent = backend_intent_from_config(LIVE_BACKEND_CONFIG, "dev")
    assert intent.pooled_models() == {"gpt-5.2": ["aif-abcdefghij123-0", "aif-abcdefghij123-1"]}


def test_backend_import_rejects_redacted_secrets() -> None:
    config = json.loads(json.dumps(LIVE_BACKEND_CONFIG))
    config[0]["authType"] = "api-key-bearer"
    config[0]["authConfig"] = {"namedValueKey": "k", "secretValue": "***redacted***"}
    with pytest.raises(ValidationError):
        backend_intent_from_config(config, "dev")


@pytest.mark.az
def test_upstream_base_access_contract_imports(layout: UpstreamLayout) -> None:
    parameters, _ = compile_params(layout.access_base_contract_dir / "main.bicepparam")
    intent, policies = access_intent_from_parameters(parameters, "dev", owners=["dev@contoso.com"])
    assert intent.services[0].code == "LLM"
    assert intent.services[0].policy_xml_file in policies
    assert "<policies>" in policies[intent.services[0].policy_xml_file]


@pytest.mark.az
def test_upstream_alert_sample_imports(layout: UpstreamLayout) -> None:
    parameters, _ = compile_params(layout.alerts_dir / "main.bicepparam")
    intent = alerts_intent_from_parameters(parameters, "dev", "gateway")
    assert [rule.name for rule in intent.alerts] == ["throttling", "quota-exceeded", "backend-failure"]
