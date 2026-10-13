# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""What-if previews and deployments of compiled Citadel contracts through Azure Resource Manager."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from azure.core.credentials import TokenCredential

from citadel_agent.azure_clients.bicep import CompiledDeployment, canonical_hash

DeploymentScope = Literal["subscription", "resourceGroup"]

_DEPLOYMENT_NAME = re.compile(r"^[-\w.()]{1,64}$")
# what-if change types that do not modify anything
NO_CHANGE_TYPES = frozenset({"NoChange", "Ignore"})


class DeploymentNameError(ValueError):
    """Raised when a deployment name is not valid for Azure Resource Manager."""


def validate_deployment_name(name: str) -> str:
    """Return ``name`` when it is a valid ARM deployment name (1-64 chars of letters, digits, -_.())."""
    if not _DEPLOYMENT_NAME.match(name):
        raise DeploymentNameError(
            f"Invalid deployment name '{name}' (1-64 characters: letters, digits, - _ . ( ))."
        )
    return name


@dataclass(frozen=True)
class DeploymentTarget:
    """Where a compiled contract is deployed."""

    scope: DeploymentScope
    subscription_id: str
    location: str | None = None
    resource_group: str | None = None

    def __post_init__(self) -> None:
        if self.scope == "subscription" and not self.location:
            raise ValueError("Subscription-scope deployments require a location.")
        if self.scope == "resourceGroup" and not self.resource_group:
            raise ValueError("Resource-group deployments require a resource group.")

    @property
    def scope_id(self) -> str:
        base = f"/subscriptions/{self.subscription_id}"
        return f"{base}/resourceGroups/{self.resource_group}" if self.scope == "resourceGroup" else base


@dataclass(frozen=True)
class WhatIfChange:
    """Predicted change to one resource. Property values are deliberately omitted.

    ``potential`` marks changes ARM could not fully evaluate (``potentialChanges``), for example
    resources inside nested deployments whose parameters depend on runtime values.
    """

    resource_id: str
    change_type: str
    property_changes: tuple[str, ...] = ()
    potential: bool = False

    @property
    def label(self) -> str:
        return f"{self.change_type} (potential)" if self.potential else self.change_type


@dataclass(frozen=True)
class WhatIfReport:
    """Summary of a what-if operation."""

    status: str
    changes: tuple[WhatIfChange, ...] = field(default=())
    error: str | None = None
    diagnostics: tuple[str, ...] = ()

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for change in self.changes:
            result[change.label] = result.get(change.label, 0) + 1
        return result

    @property
    def has_changes(self) -> bool:
        return any(change.change_type not in NO_CHANGE_TYPES for change in self.changes)

    def fingerprint(self) -> str:
        """Stable hash of the predicted changes, used to detect drift between preview and execution.

        Only resource IDs, change types and property paths are hashed. Values are excluded, so
        deterministic what-if noise (for example APIM policies that always show as modified) does
        not cause false drift.
        """
        material = sorted(
            [c.resource_id.lower(), c.change_type, c.potential, sorted(c.property_changes)]
            for c in self.changes
            if c.change_type not in NO_CHANGE_TYPES
        )
        return canonical_hash(material)


@dataclass(frozen=True)
class DeploymentRecord:
    """State of an ARM deployment."""

    name: str
    provisioning_state: str
    outputs: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    timestamp: str | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.provisioning_state == "Succeeded"

    def output(self, name: str) -> Any:
        """Return the value of a deployment output, or None when it does not exist."""
        entry = self.outputs.get(name)
        return entry.get("value") if isinstance(entry, Mapping) else entry


def _as_dict(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {k: _as_dict(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_as_dict(v) for v in value]
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return value


def _flatten_property_changes(deltas: Iterable[Mapping[str, Any]], prefix: str = "") -> list[str]:
    flattened: list[str] = []
    for delta in deltas or []:
        path = delta.get("path", "")
        full_path = f"{prefix}.{path}" if prefix and path else (path or prefix)
        change_type = delta.get("property_change_type") or delta.get("propertyChangeType") or "Modify"
        children = delta.get("children") or []
        if children:
            flattened.extend(_flatten_property_changes(children, full_path))
        else:
            flattened.append(f"{full_path} ({change_type})")
    return flattened


def _section(data: Mapping[str, Any], *keys: str) -> list[Any]:
    """Read a list from the flattened SDK shape or from ``properties`` (the REST wire shape)."""
    nested = data.get("properties")
    properties: Mapping[str, Any] = nested if isinstance(nested, Mapping) else {}
    for key in keys:
        value = data.get(key)
        if value is None:
            value = properties.get(key)
        if value:
            return list(value)
    return []


def _what_if_change(change: Mapping[str, Any], *, potential: bool) -> WhatIfChange:
    return WhatIfChange(
        resource_id=change.get("resource_id") or change.get("resourceId") or "",
        change_type=str(change.get("change_type") or change.get("changeType") or "Unknown"),
        property_changes=tuple(_flatten_property_changes(change.get("delta") or [])),
        potential=potential,
    )


def _diagnostic(entry: Mapping[str, Any]) -> str:
    level = entry.get("level") or "Info"
    code = entry.get("code") or ""
    message = str(entry.get("message") or "").strip()
    target = entry.get("target")
    text = f"{level} {code}: {message}".strip()
    return f"{text} ({target})" if target else text


def parse_what_if_result(result: Any) -> WhatIfReport:
    """Convert an SDK or REST what-if result into a :class:`WhatIfReport`.

    Accepts both the flattened shape (older SDKs: ``changes`` at the top level) and the REST wire
    shape returned by ``as_dict()`` of current SDK models (``properties.changes``).
    """
    data = _as_dict(result) or {}
    error = data.get("error") or (data.get("properties") or {}).get("error")
    changes = [_what_if_change(c, potential=False) for c in _section(data, "changes")]
    changes += [
        _what_if_change(c, potential=True) for c in _section(data, "potentialChanges", "potential_changes")
    ]
    return WhatIfReport(
        status=str(data.get("status") or ("Failed" if error else "Succeeded")),
        changes=tuple(changes),
        error=(error.get("message") if isinstance(error, Mapping) else str(error)) if error else None,
        diagnostics=tuple(_diagnostic(d) for d in _section(data, "diagnostics") if isinstance(d, Mapping)),
    )


def _error_text(error: Any, *, limit: int = 2000) -> str | None:
    """Flatten an ARM error (with nested ``details``) into one readable string."""
    if not error:
        return None
    if not isinstance(error, Mapping):
        return str(error)[:limit]
    parts: list[str] = []

    def visit(node: Mapping[str, Any]) -> None:
        code = node.get("code")
        message = str(node.get("message") or "").strip()
        if message or code:
            parts.append(f"{code}: {message}" if code else message)
        for child in node.get("details") or []:
            if isinstance(child, Mapping):
                visit(child)

    visit(error)
    text = " | ".join(dict.fromkeys(parts))
    return text[:limit] if text else None


def parse_deployment(result: Any) -> DeploymentRecord:
    """Convert an SDK or REST deployment into a :class:`DeploymentRecord`."""
    data = _as_dict(result) or {}
    properties = data.get("properties") or {}
    return DeploymentRecord(
        name=data.get("name", ""),
        provisioning_state=str(
            properties.get("provisioning_state") or properties.get("provisioningState") or ""
        ),
        outputs=properties.get("outputs") or {},
        correlation_id=properties.get("correlation_id") or properties.get("correlationId"),
        timestamp=str(properties.get("timestamp")) if properties.get("timestamp") else None,
        error=_error_text(properties.get("error")),
    )


class DeploymentRunner:
    """Runs what-if and deployments for compiled contracts."""

    def __init__(self, credential: TokenCredential, subscription_id: str, *, client: Any = None) -> None:
        if client is None:
            from azure.mgmt.resource.deployments import DeploymentsMgmtClient

            client = DeploymentsMgmtClient(credential, subscription_id)
        self._client = client

    @staticmethod
    def _body(target: DeploymentTarget, compiled: CompiledDeployment, *, what_if: bool) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "mode": "Incremental",
            "template": compiled.template,
            "parameters": compiled.parameters,
        }
        if what_if:
            properties["whatIfSettings"] = {"resultFormat": "FullResourcePayloads"}
        body: dict[str, Any] = {"properties": properties}
        if target.scope == "subscription":
            body["location"] = target.location
        return body

    def what_if(self, target: DeploymentTarget, name: str, compiled: CompiledDeployment) -> WhatIfReport:
        """Preview the changes a deployment would make. Does not modify any resource."""
        validate_deployment_name(name)
        body = self._body(target, compiled, what_if=True)
        deployments = self._client.deployments
        if target.scope == "subscription":
            poller = deployments.begin_what_if_at_subscription_scope(name, body)
        else:
            poller = deployments.begin_what_if(target.resource_group, name, body)
        return parse_what_if_result(poller.result())

    def deploy(
        self,
        target: DeploymentTarget,
        name: str,
        compiled: CompiledDeployment,
        *,
        timeout: float | None = None,
    ) -> DeploymentRecord:
        """Deploy the compiled contract and wait for completion.

        A failed deployment is returned as a record with its error (including nested ARM error
        details) instead of raising, so callers can record the failure.
        """
        from azure.core.exceptions import HttpResponseError

        validate_deployment_name(name)
        body = self._body(target, compiled, what_if=False)
        deployments = self._client.deployments
        if target.scope == "subscription":
            poller = deployments.begin_create_or_update_at_subscription_scope(name, body)
        else:
            poller = deployments.begin_create_or_update(target.resource_group, name, body)
        try:
            return parse_deployment(poller.result(timeout=timeout))
        except HttpResponseError as exc:
            message = _error_text(_as_dict(getattr(exc, "error", None))) or str(exc)
            try:
                record = self.get(target, name)
            except HttpResponseError:
                record = DeploymentRecord(name=name, provisioning_state="Failed")
            return replace(
                record,
                provisioning_state=record.provisioning_state or "Failed",
                error=record.error or message,
            )

    def get(self, target: DeploymentTarget, name: str) -> DeploymentRecord:
        """Return the current state of a deployment."""
        deployments = self._client.deployments
        if target.scope == "subscription":
            return parse_deployment(deployments.get_at_subscription_scope(name))
        return parse_deployment(deployments.get(target.resource_group, name))
