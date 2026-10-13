# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Typed views of Citadel gateway responses."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from citadel_agent.safety.secret_ingress import UPSTREAM_REDACTION_SENTINEL


@dataclass(frozen=True)
class GatewayError:
    """An error returned by the gateway or a backend, normalized from several body shapes."""

    status_code: int
    message: str
    code: str | None = None
    error_type: str | None = None
    param: str | None = None


@dataclass(frozen=True)
class GatewayResponseInfo:
    """Diagnostics extracted from a gateway response (never includes request credentials)."""

    status_code: int
    latency_ms: float
    request_id: str | None = None
    backend_request_id: str | None = None
    region: str | None = None
    retry_after: str | None = None
    remaining_tokens: str | None = None
    remaining_requests: str | None = None
    debug_headers: Mapping[str, str] = field(default_factory=dict)
    error: GatewayError | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass(frozen=True)
class ModelInfo:
    """A model or alias the gateway can route to."""

    name: str
    kind: str
    model_format: str | None = None
    version: str | None = None
    sku: str | None = None
    capacity: int | None = None
    backend_type: str | None = None
    retirement_date: str | None = None


@dataclass(frozen=True)
class SmokeTestResult:
    """Outcome of a minimal call through the gateway."""

    operation: str
    model: str
    info: GatewayResponseInfo
    usage: Mapping[str, Any] | None = None
    preview: str | None = None

    @property
    def ok(self) -> bool:
        return self.info.ok


class BackendContractView:
    """Read-only view of the live backend routing contract (``GET /version/backend-contract``)."""

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self.raw = dict(raw)

    @property
    def master_version(self) -> str | None:
        return self.raw.get("masterVersion")

    @property
    def backend_contract_version(self) -> str | None:
        return self.raw.get("backendContractVersion")

    @property
    def circuit_breaker_enabled(self) -> bool:
        return bool((self.raw.get("circuitBreaker") or {}).get("enabled"))

    @property
    def session_affinity_enabled(self) -> bool:
        return bool((self.raw.get("sessionAffinity") or {}).get("enabled"))

    def backends(self) -> list[dict[str, Any]]:
        return list(self.raw.get("backends") or [])

    def pools(self) -> list[dict[str, Any]]:
        return list(self.raw.get("pools") or [])

    def aliases(self) -> list[dict[str, Any]]:
        return list(self.raw.get("modelAliases") or [])

    def model_names(self) -> set[str]:
        """Return all routable model names (case preserved) including aliases."""
        names = {
            m["name"] for b in self.backends() for m in b.get("supportedModels", []) if isinstance(m, Mapping)
        }
        names.update(m for b in self.backends() for m in b.get("supportedModels", []) if isinstance(m, str))
        names.update(a["name"] for a in self.aliases() if "name" in a)
        return names

    def has_model(self, name: str) -> bool:
        lowered = name.lower()
        return any(model.lower() == lowered for model in self.model_names())

    def backends_for_model(self, name: str) -> list[str]:
        """Return the IDs of backends that serve ``name`` (case-insensitive)."""
        lowered = name.lower()
        result = []
        for backend in self.backends():
            for model in backend.get("supportedModels", []):
                model_name = model.get("name") if isinstance(model, Mapping) else model
                if isinstance(model_name, str) and model_name.lower() == lowered:
                    result.append(backend.get("backendId", ""))
        return result

    def has_redacted_secrets(self) -> bool:
        """True when any backend carries an inline secret (shown redacted); never redeploy such config."""
        return any(
            (backend.get("authConfig") or {}).get("secretValue") == UPSTREAM_REDACTION_SENTINEL
            for backend in self.backends()
        )
