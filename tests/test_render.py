# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the .bicepparam serializer and contract renderers (with golden Bicep compiles)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from citadel_agent.azure_clients.bicep import compile_deployment, compile_params
from citadel_agent.contracts import parse_intent
from citadel_agent.contracts.bicepparam import BicepExpression, bicep_string, render_param_file, to_bicep
from citadel_agent.contracts.render import RenderContext, RenderError, render_intent
from citadel_agent.contracts.targets import EnvironmentTarget

SOURCE_UPSTREAM = Path(__file__).resolve().parents[1] / "upstream" / "ai-hub-gateway"
SUB = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
UAMI_CLIENT = "11111111-2222-3333-4444-555555555555"


def target(**overrides: Any) -> EnvironmentTarget:
    data: dict[str, Any] = {
        "name": "dev",
        "environmentClass": "dev",
        "subscriptionId": SUB,
        "location": "swedencentral",
        "tenantId": TENANT,
        "apim": {"subscriptionId": SUB, "resourceGroupName": "rg-citadel-agent-dev", "name": "apim-dev"},
        "gatewayUrl": "https://apim-dev.azure-api.net",
        "apimManagedIdentity": {
            "subscriptionId": SUB,
            "resourceGroupName": "rg-citadel-agent-dev",
            "name": "id-apim-dev",
            "clientId": UAMI_CLIENT,
        },
        "keyVault": {"subscriptionId": SUB, "resourceGroupName": "rg-citadel-agent-dev", "name": "kv-dev"},
        "appInsights": {
            "subscriptionId": SUB,
            "resourceGroupName": "rg-citadel-agent-dev",
            "name": "appi-apim-dev",
        },
    }
    data.update(overrides)
    return EnvironmentTarget.model_validate(data)


@pytest.fixture(scope="session")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A temporary canonical repository with a copy of the pinned upstream templates."""
    if not (SOURCE_UPSTREAM / "release.json").is_file():
        pytest.skip("Upstream submodule is not checked out")
    root = tmp_path_factory.mktemp("canonical-repo")
    upstream = root / "upstream" / "ai-hub-gateway"
    shutil.copytree(SOURCE_UPSTREAM / "bicep" / "infra", upstream / "bicep" / "infra")
    shutil.copy2(SOURCE_UPSTREAM / "release.json", upstream / "release.json")
    return root


def context(repo: Path, **overrides: Any) -> RenderContext:
    return RenderContext(
        repo_root=repo,
        upstream_root=repo / "upstream" / "ai-hub-gateway",
        target=overrides.pop("target", target()),
        upstream_commit="23fbc8fe7f2f068de3ed3c80da2344760faac1e6",
        **overrides,
    )


ACCESS = {
    "kind": "access-contract",
    "environment": "dev",
    "metadata": {"owners": ["dev@contoso.com"]},
    "useCase": {"businessUnit": "HR", "useCaseName": "ChatAgent", "environment": "DEV"},
    "services": [
        {
            "code": "LLM",
            "apis": ["universal-llm-api", "azure-openai-api", "unified-ai-api"],
            "keySecretName": "HR-CHATAGENT-LLM-KEY",
            "endpointSecretName": "HR-CHATAGENT-LLM-ENDPOINT",
            "policy": {"allowedModels": ["gpt-5.2"], "tokenLimit": {"tokensPerMinute": 5000}},
        }
    ],
    "foundry": {
        "enabled": True,
        "project": {
            "subscriptionId": SUB,
            "resourceGroupName": "rg-citadel-agent-dev",
            "accountName": "aif-dev-0",
            "projectName": "citadel-governance-project",
        },
    },
}
BACKEND = {
    "kind": "backend-contract",
    "environment": "dev",
    "backends": [
        {
            "backendId": "aif-dev-0",
            "backendType": "ai-foundry",
            "endpoint": "https://aif-dev-0.cognitiveservices.azure.com/",
            "authType": "managed-identity",
            "supportedModels": [{"name": "gpt-5.2", "sku": "GlobalStandard", "capacity": 100}],
        },
        {
            "backendId": "aif-dev-1",
            "backendType": "ai-foundry",
            "endpoint": "https://aif-dev-1.cognitiveservices.azure.com/",
            "supportedModels": [{"name": "gpt-5.2"}, {"name": "gpt-5.4-mini"}],
            "priority": 2,
        },
    ],
    "modelAliases": [{"name": "smart", "models": ["gpt-5.2", "gpt-5.4-mini"]}],
}
PUBLISH = {
    "kind": "publish-contract",
    "environment": "dev",
    "name": "learn-tools",
    "assets": [
        {
            "assetType": "mcp-existing",
            "name": "ms-learn-tool",
            "displayName": "Microsoft Learn Tool (MCP)",
            "path": "ms-learn-tool-mcp",
            "transportType": "streamable",
            "subscriptionRequired": True,
            "backend": {"url": "https://learn.microsoft.com/api/mcp", "authType": "none"},
        }
    ],
}
ALERTS = {
    "kind": "alerts",
    "environment": "dev",
    "scopeName": "hr-chatagent",
    "alertEmailAddress": "ops@contoso.com",
    "productDisplayNameFilter": "LLM HR ChatAgent DEV",
}


# Serializer ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("it's", "'it\\'s'"),
        ("a\\b", "'a\\\\b'"),
        ("${x}", "'\\${x}'"),
        ("line1\nline2", "'line1\\nline2'"),
    ],
)
def test_bicep_string_escaping(value: str, expected: str) -> None:
    assert bicep_string(value) == expected


def test_to_bicep_structures() -> None:
    value = {
        "a-b": [1, True, None],
        "nested": {"x": 0.8, "y": 2.0},
        "expr": BicepExpression("loadTextContent('p.xml')"),
    }
    assert to_bicep(value) == (
        "{\n"
        "  'a-b': [\n    1\n    true\n    null\n  ]\n"
        "  nested: {\n    x: json('0.8')\n    y: 2\n  }\n"
        "  expr: loadTextContent('p.xml')\n"
        "}"
    )
    assert to_bicep([]) == "[]" and to_bicep({}) == "{}"


def test_render_param_file_rejects_bad_names() -> None:
    with pytest.raises(ValueError):
        render_param_file("main.bicep", {"bad-name": 1})


# Renderers -------------------------------------------------------------------------------------


def test_access_contract_render_layout_and_parameters(repo: Path) -> None:
    rendered = render_intent(parse_intent(ACCESS), context(repo))
    directory = PurePosixPath("contracts/access/hr/chatagent/dev")
    assert rendered.directory == directory
    assert set(rendered.files) == {
        directory / "main.bicepparam",
        directory / "llm-product-policy.xml",
        directory / "contract.yaml",
    }
    params = rendered.files[directory / "main.bicepparam"]
    assert (
        "using '../../../../../upstream/ai-hub-gateway/bicep/infra/citadel-access-contracts/main.bicep'"
        in params
    )
    assert "policyXml: loadTextContent('llm-product-policy.xml')" in params
    assert "inferenceAPIVersion: ''" in params  # upstream key casing preserved
    assert "upstream commit 23fbc8fe7f2f" in params
    policy = rendered.files[directory / "llm-product-policy.xml"]
    assert f"https://sts.windows.net/{TENANT}/" in policy  # Foundry managed identity JWT override
    assert rendered.deployment.template == PurePosixPath(
        "upstream/ai-hub-gateway/bicep/infra/citadel-access-contracts/main.bicep"
    )
    assert rendered.deployment.name_base == "citadel-access-hr-chatagent-dev"


def test_render_requires_matching_environment_and_resources(repo: Path) -> None:
    with pytest.raises(RenderError, match="environment"):
        render_intent(parse_intent(dict(ACCESS, environment="prod")), context(repo))
    with pytest.raises(RenderError, match="Key Vault"):
        render_intent(parse_intent(ACCESS), context(repo, target=target(keyVault=None)))
    with pytest.raises(RenderError, match="managed identity"):
        render_intent(parse_intent(BACKEND), context(repo, target=target(apimManagedIdentity=None)))
    with pytest.raises(RenderError, match="Application Insights"):
        render_intent(parse_intent(ALERTS), context(repo, target=target(appInsights=None)))


def test_alerts_render_uses_scope_specific_prefix(repo: Path) -> None:
    rendered = render_intent(parse_intent(ALERTS), context(repo))
    params = rendered.files[rendered.deployment.parameters_file]
    assert "param namePrefix = 'aigw-hr-chatagent'" in params
    assert "param productNameFilter = 'LLM HR ChatAgent DEV'" in params


def test_written_files_use_lf_line_endings(repo: Path, tmp_path: Path) -> None:
    rendered = render_intent(parse_intent(PUBLISH), context(repo))
    for path in rendered.write(tmp_path):
        assert b"\r\n" not in path.read_bytes()


# Golden compiles against the pinned upstream templates ----------------------------------------


@pytest.mark.parametrize(
    "intent_data", [ACCESS, BACKEND, PUBLISH, ALERTS], ids=["access", "backend", "publish", "alerts"]
)
def test_extra_parameters_cannot_override_what_the_renderer_computes(
    repo: Path, intent_data: dict[str, Any]
) -> None:
    clean = render_intent(parse_intent(intent_data), context(repo))
    computed = sorted(
        name
        for name in ("apim", "keyVault", "services", "llmBackendConfig", "publishAssets", "alerts")
        if f"param {name} " in clean.files[clean.deployment.parameters_file]
    )
    assert computed, "the fixture must compute at least one parameter"

    for name in computed:
        hostile = parse_intent({**intent_data, "extraParameters": {name: "attacker-value"}})
        with pytest.raises(RenderError, match=f"cannot override.*{name}"):
            render_intent(hostile, context(repo))


def test_extra_parameters_may_add_upstream_parameters_the_schema_does_not_model(repo: Path) -> None:
    intent = parse_intent({**ACCESS, "extraParameters": {"someNewUpstreamFlag": True}})

    rendered = render_intent(intent, context(repo))

    assert "someNewUpstreamFlag" in rendered.files[rendered.deployment.parameters_file]


@pytest.mark.az
@pytest.mark.parametrize(
    "intent_data", [ACCESS, BACKEND, PUBLISH, ALERTS], ids=["access", "backend", "publish", "alerts"]
)
def test_rendered_parameters_compile_against_upstream(repo: Path, intent_data: dict[str, Any]) -> None:
    rendered = render_intent(parse_intent(intent_data), context(repo))
    rendered.write(repo)
    parameters, diagnostics = compile_params(repo / Path(*rendered.deployment.parameters_file.parts))
    assert not [d for d in diagnostics if d.level == "Error"]
    if intent_data is ACCESS:
        assert parameters["useCase"]["value"]["businessUnit"] == "HR"
        policy = parameters["services"]["value"][0]["policyXml"]
        assert "<policies>" in policy  # loadTextContent resolved the generated policy file
    if intent_data is BACKEND:
        assert parameters["modelAliases"]["value"][0]["name"] == "smart"
    json.dumps(parameters)  # fully JSON serializable


@pytest.mark.az
def test_access_contract_full_compile(repo: Path) -> None:
    rendered = render_intent(parse_intent(ACCESS), context(repo))
    rendered.write(repo)
    compiled = compile_deployment(repo / Path(*rendered.deployment.parameters_file.parts))
    assert compiled.template_file.name == "main.bicep"
    assert "resources" in compiled.template
