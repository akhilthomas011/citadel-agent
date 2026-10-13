# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Detect drift between the agent's contract models and the pinned upstream templates.

Upstream contract templates use untyped ``object``/``array`` parameters, so the agent maintains its
own models of their inner shapes. This module checks those models against upstream:

* parameter surface: every parameter the agent renders must exist upstream with the same type,
  and every required upstream parameter must be rendered
* property usage: every key the agent renders inside an object must be read by upstream Bicep,
  and properties upstream reads but the agent cannot set are reported as warnings

Run it in CI and during ``citadel-agent upstream sync``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from citadel_agent.azure_clients.bicep import compile_template
from citadel_agent.contracts.alerts import AlertRule
from citadel_agent.contracts.backend import BackendEntry, ModelAlias, ModelEntry
from citadel_agent.contracts.publish import PublishAsset, PublishBackend, PublishBackendAuthConfig
from citadel_agent.upstream.layout import UpstreamLayout


class DriftLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class DriftFinding:
    level: DriftLevel
    area: str
    template: str
    message: str


# Top-level parameters the renderers emit, with their ARM types.
AGENT_PARAMETERS: dict[str, dict[str, str]] = {
    "access": {
        "apim": "object",
        "keyVault": "object",
        "useTargetAzureKeyVault": "bool",
        "useCase": "object",
        "apiNameMapping": "object",
        "services": "array",
        "productTerms": "string",
        "usePrimaryKey": "bool",
        "keyRotationEnabled": "bool",
        "useTargetFoundry": "bool",
        "foundry": "object",
        "foundryConfig": "object",
        "globalGatewayUrl": "string",
        "additionalApimGateways": "array",
        "additionalKeyVaults": "array",
        "additionalFoundries": "array",
    },
    "backend": {
        "apim": "object",
        "apimManagedIdentity": "object",
        "llmBackendConfig": "array",
        "configureCircuitBreaker": "bool",
        "circuitBreakerDefaults": "object",
        "configureSessionAffinity": "bool",
        "sessionAffinityDefaults": "object",
        "anthropicVersion": "string",
        "modelAliases": "array",
        "keyVaultName": "string",
    },
    "publish": {
        "apim": "object",
        "managedIdentityClientId": "string",
        "configureCircuitBreaker": "bool",
        "circuitBreakerDefaults": "object",
        "ensureUsageFragments": "bool",
        "useAssetTypePathPrefix": "bool",
        "apiCenter": "object",
        "publishAssets": "array",
    },
    "alerts": {
        "appInsights": "object",
        "alertEmailAddress": "string",
        "additionalEmailReceivers": "array",
        "productNameFilter": "string",
        "alertMode": "string",
        "namePrefix": "string",
        "actionGroupShortName": "string",
        "alertsEnabled": "bool",
        "alerts": "array",
    },
}


def _aliases(model: type, *, add: Iterable[str] = (), remove: Iterable[str] = ()) -> frozenset[str]:
    names = {field.alias or name for name, field in model.model_fields.items()}  # type: ignore[attr-defined]
    return frozenset((names | set(add)) - set(remove))


@dataclass(frozen=True)
class PropertyScan:
    """Which upstream files read one rendered object, through which variable paths."""

    name: str
    files: tuple[str, ...]
    variables: tuple[str, ...]
    rendered_keys: frozenset[str]
    # Keys upstream reads that the agent leaves unset on purpose (derived upstream, or forbidden).
    ignore_upstream: frozenset[str] = frozenset()


# Keys the agent renders for each upstream object (renderers map them explicitly where needed).
PROPERTY_SCANS: tuple[PropertyScan, ...] = (
    PropertyScan(
        name="access.services[]",
        files=("citadel-access-contracts/main.bicep",),
        variables=("s", "item.s"),
        rendered_keys=frozenset(
            {
                "code",
                "apiKeySecretName",
                "endpointSecretName",
                "assetEndpoints",
                "publishAllAssetEndpoints",
                "foundryApiName",
                "policyXml",
            }
        ),
    ),
    PropertyScan(
        name="access.foundryConfig",
        files=("citadel-access-contracts/main.bicep",),
        variables=("foundryConfig",),
        rendered_keys=frozenset(
            {
                "connectionNamePrefix",
                "connectionCategory",
                "authType",
                "managedIdentityAudience",
                "deploymentInPath",
                "isSharedToAll",
                "inferenceAPIVersion",
                "deploymentAPIVersion",
                "staticModels",
                "listModelsEndpoint",
                "getModelEndpoint",
                "deploymentProvider",
                "customHeaders",
                "authConfig",
            }
        ),
    ),
    PropertyScan(
        name="backend.llmBackendConfig[]",
        files=("llm-backend-onboarding/modules/llm-backends.bicep",),
        variables=("config", "entry.raw"),
        rendered_keys=_aliases(BackendEntry),
    ),
    PropertyScan(
        name="backend.supportedModels[]",
        files=("llm-backend-onboarding/modules/llm-policy-fragments.bicep",),
        variables=("model",),
        rendered_keys=_aliases(ModelEntry, remove={"sessionAwareModel"}),
    ),
    PropertyScan(
        name="backend.modelAliases[]",
        files=("llm-backend-onboarding/modules/llm-policy-fragments.bicep",),
        variables=("alias",),
        rendered_keys=_aliases(ModelAlias),
    ),
    PropertyScan(
        name="publish.publishAssets[]",
        files=("citadel-publish-contracts/main.bicep",),
        variables=("asset",),
        rendered_keys=_aliases(PublishAsset, add={"policyXml"}, remove={"policyXmlFile", "metadata"}),
    ),
    PropertyScan(
        name="publish.backend",
        files=("citadel-publish-contracts/main.bicep", "citadel-publish-contracts/modules/publishBackend.bicep"),
        variables=("asset.backend", "backend"),
        rendered_keys=_aliases(PublishBackend),
        ignore_upstream=frozenset({"name"}),
    ),
    PropertyScan(
        name="publish.backend.authConfig",
        files=("citadel-publish-contracts/main.bicep", "citadel-publish-contracts/modules/publishBackend.bicep"),
        variables=("authConfig", "asset.backend.authConfig", "backend.authConfig"),
        rendered_keys=_aliases(PublishBackendAuthConfig),
        ignore_upstream=frozenset({"secretValue"}),
    ),
    PropertyScan(
        name="alerts.alerts[]",
        files=("app-insights-alert/main.bicep",),
        variables=("alert",),
        rendered_keys=_aliases(AlertRule),
    ),
)

def _interpolations(literal: str) -> list[str]:
    """Return the ``${...}`` expressions inside a single-quoted Bicep string literal."""
    expressions: list[str] = []
    position = 0
    while True:
        start = literal.find("${", position)
        if start < 0:
            return expressions
        depth, cursor = 0, start + 1
        while cursor < len(literal):
            if literal[cursor] == "{":
                depth += 1
            elif literal[cursor] == "}":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        expressions.append(literal[start + 2 : cursor])
        position = cursor + 1


def _scan_bicep(text: str, *, keep_strings: bool) -> str:
    """Remove comments (and optionally string contents) using a single-pass Bicep tokenizer.

    Comments are only recognized outside strings, so URLs such as 'https://...' are preserved.
    When strings are removed, their ``${...}`` interpolations are kept because they are code.
    """
    out: list[str] = []
    position, length = 0, len(text)
    while position < length:
        if text.startswith("'''", position):
            end = text.find("'''", position + 3)
            stop = length if end < 0 else end + 3
            out.append(text[position:stop] if keep_strings else "''")
            position = stop
            continue
        char = text[position]
        if char == "'":
            end = position + 1
            while end < length and text[end] != "'":
                end += 2 if text[end] == "\\" else 1
            stop = min(end + 1, length)
            literal = text[position:stop]
            if keep_strings:
                out.append(literal)
            else:
                out.append("'' " + " ".join(_interpolations(literal)) + " ")
            position = stop
            continue
        if text.startswith("//", position):
            newline = text.find("\n", position)
            position = length if newline < 0 else newline
            continue
        if text.startswith("/*", position):
            end = text.find("*/", position + 2)
            position = length if end < 0 else end + 2
            continue
        out.append(char)
        position += 1
    return "".join(out)


def upstream_properties(text: str, variables: Iterable[str]) -> set[str]:
    """Return property names accessed on Bicep variables (``x.prop``, ``x.?prop``, ``contains(x, 'p')``)."""
    with_strings = _scan_bicep(text, keep_strings=True)
    code = _scan_bicep(text, keep_strings=False)
    found: set[str] = set()
    for variable in variables:
        escaped = re.escape(variable)
        found.update(re.findall(rf"(?<![\w.]){escaped}\.\??([A-Za-z_]\w*)", code))
        found.update(re.findall(rf"contains\(\s*{escaped}\s*,\s*'([A-Za-z_]\w*)'\s*\)", with_strings))
    return found


def check_property_usage(layout: UpstreamLayout) -> list[DriftFinding]:
    """Compare the keys the agent renders with the properties upstream Bicep reads."""
    findings: list[DriftFinding] = []
    for scan in PROPERTY_SCANS:
        used: set[str] = set()
        for relative in scan.files:
            path = layout.infra / relative
            if not path.is_file():
                findings.append(DriftFinding(DriftLevel.ERROR, "properties", relative, "file not found upstream"))
                continue
            used |= upstream_properties(path.read_text(encoding="utf-8"), scan.variables)
        unread = sorted(scan.rendered_keys - used)
        if unread:
            findings.append(
                DriftFinding(
                    DriftLevel.ERROR,
                    "properties",
                    scan.name,
                    f"agent renders keys upstream never reads: {', '.join(unread)}",
                )
            )
        unsupported = sorted(used - scan.rendered_keys - scan.ignore_upstream)
        if unsupported:
            findings.append(
                DriftFinding(
                    DriftLevel.WARNING,
                    "properties",
                    scan.name,
                    f"upstream reads keys the agent cannot set: {', '.join(unsupported)}",
                )
            )
    return findings


def template_parameters(template: Path) -> dict[str, dict[str, object]]:
    """Compile an upstream template and return its ARM ``parameters`` section."""
    compiled, _ = compile_template(template)
    return dict(compiled.get("parameters", {}))


def check_parameter_surface(layout: UpstreamLayout) -> list[DriftFinding]:
    """Compare the parameters the agent renders with each upstream template's parameters."""
    findings: list[DriftFinding] = []
    for kind, template in layout.contract_templates().items():
        upstream = template_parameters(template)
        agent = AGENT_PARAMETERS[kind]
        for name, expected_type in agent.items():
            if name not in upstream:
                findings.append(
                    DriftFinding(DriftLevel.ERROR, "parameters", kind, f"'{name}' no longer exists upstream")
                )
                continue
            actual = str(upstream[name].get("type", "")).lower()
            if actual.replace("secure", "") != expected_type:
                findings.append(
                    DriftFinding(
                        DriftLevel.ERROR,
                        "parameters",
                        kind,
                        f"'{name}' changed type from {expected_type} to {actual}",
                    )
                )
        for name, definition in upstream.items():
            if name in agent:
                continue
            if "defaultValue" not in definition:
                findings.append(
                    DriftFinding(DriftLevel.ERROR, "parameters", kind, f"new required parameter '{name}' upstream")
                )
            else:
                findings.append(
                    DriftFinding(DriftLevel.WARNING, "parameters", kind, f"new optional parameter '{name}' upstream")
                )
    return findings


# Upstream parameters the agent intentionally never renders (defaults are correct or values are secret).
INTENTIONALLY_UNRENDERED: dict[str, frozenset[str]] = {
    "access": frozenset({"rotationKeySeed", "rotationKeyOverride"}),
    "backend": frozenset({"awsAccessKey", "awsSecretKey", "awsRegion"}),
    "publish": frozenset({"appInsightsLoggerName"}),
    "alerts": frozenset({"alertResourceGroupName", "metricNamespace", "metricName"}),
}


def filter_known(findings: list[DriftFinding]) -> list[DriftFinding]:
    """Drop warnings about parameters the agent leaves unset on purpose."""
    result = []
    for finding in findings:
        skip = finding.area == "parameters" and finding.level is DriftLevel.WARNING and any(
            f"'{name}'" in finding.message for name in INTENTIONALLY_UNRENDERED.get(finding.template, ())
        )
        if not skip:
            result.append(finding)
    return result


def check_knowledge(layout: UpstreamLayout) -> list[DriftFinding]:
    """Catalog evidence strings and documentation anchors that no longer exist upstream."""
    from citadel_agent.knowledge import build_doc_index, catalog_drift, load_catalogs

    problems = catalog_drift(load_catalogs(), layout.root, build_doc_index(layout.root))
    return [DriftFinding(DriftLevel.ERROR, "knowledge", "catalogs", problem) for problem in problems]


def run_drift_checks(layout: UpstreamLayout, *, compile_templates: bool = True) -> list[DriftFinding]:
    """Run all drift checks. Template compilation (needs the Bicep CLI) can be skipped."""
    findings = check_property_usage(layout)
    findings.extend(check_knowledge(layout))
    if compile_templates:
        findings.extend(check_parameter_surface(layout))
    return filter_known(findings)
