# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Citadel gateway data-plane client.

Covers the anonymous release endpoints (``/version`` and ``/version/backend-contract``), model
discovery through the Unified AI API and minimal smoke tests. Subscription keys are sent in the
``api-key`` header, registered with the redactor and never returned.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

import httpx

from citadel_agent.gateway.models import (
    BackendContractView,
    GatewayError,
    GatewayResponseInfo,
    ModelInfo,
    SmokeTestResult,
)
from citadel_agent.safety.redaction import Redactor, default_redactor

UNIFIED_AI_PREFIX = "/unified-ai"
DEBUG_HEADER_PREFIX = "uaig-"
SMOKE_TEST_PROMPT = "Reply with the single word OK."
DEFAULT_TIMEOUT = httpx.Timeout(60.0)


def parse_error(status_code: int, body: Any) -> GatewayError:
    """Normalize the error body shapes used by APIM, Citadel policies and model providers."""
    if isinstance(body, Mapping):
        inner = body.get("error")
        if isinstance(inner, Mapping):
            return GatewayError(
                status_code=status_code,
                message=str(inner.get("message") or ""),
                code=str(inner["code"]) if inner.get("code") is not None else None,
                error_type=inner.get("type"),
                param=inner.get("param"),
            )
        if isinstance(inner, str):
            return GatewayError(status_code=status_code, message=inner)
        if "message" in body:
            return GatewayError(
                status_code=status_code,
                message=str(body.get("message")),
                code=str(body["code"]) if body.get("code") is not None else None,
            )
    text = body if isinstance(body, str) else json.dumps(body)[:500] if body is not None else ""
    return GatewayError(status_code=status_code, message=text[:500])


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def response_info(response: httpx.Response, latency_ms: float) -> GatewayResponseInfo:
    """Extract diagnostics from a gateway response."""
    headers = response.headers
    body = None if response.is_success else _decode(response)
    return GatewayResponseInfo(
        status_code=response.status_code,
        latency_ms=round(latency_ms, 1),
        request_id=headers.get("uaig-request-id") or headers.get("x-ms-request-id"),
        backend_request_id=headers.get("apim-request-id") or headers.get("x-request-id"),
        region=headers.get("x-ms-region"),
        retry_after=headers.get("retry-after"),
        # The product's own token limit first; x-ratelimit-* headers come from the model backend.
        remaining_tokens=headers.get("remaining-tokens") or headers.get("x-ratelimit-remaining-tokens"),
        remaining_requests=headers.get("x-ratelimit-remaining-requests"),
        debug_headers={k: v for k, v in headers.items() if k.lower().startswith(DEBUG_HEADER_PREFIX)},
        error=None if response.is_success else parse_error(response.status_code, body),
    )


def parse_deployments(document: Any) -> list[ModelInfo]:
    """Parse the ``GET /unified-ai/deployments`` discovery document."""
    items = document.get("value", []) if isinstance(document, Mapping) else []
    models: list[ModelInfo] = []
    for item in items:
        props = item.get("properties") or {}
        model = props.get("model") or {}
        sku = item.get("sku") or {}
        model_format = model.get("format")
        is_alias = item.get("type") == "alias" or model_format == "Alias"
        capacity = sku.get("capacity")
        models.append(
            ModelInfo(
                name=item.get("name") or model.get("name") or "",
                kind="alias" if is_alias else "model",
                model_format=model_format,
                version=model.get("version"),
                sku=sku.get("name"),
                capacity=int(capacity)
                if isinstance(capacity, int | str) and str(capacity).isdigit()
                else None,
                backend_type=None if is_alias else item.get("type"),
                retirement_date=props.get("retirementDate") or model.get("retirementDate"),
            )
        )
    return models


class GatewayClient:
    """Client for one Citadel gateway (APIM gateway URL)."""

    def __init__(
        self,
        gateway_url: str,
        *,
        http_client: httpx.Client | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self._http = http_client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._redactor = redactor or default_redactor()

    def _headers(self, api_key: str | None, bearer_token: str | None) -> dict[str, str]:
        headers = {"Accept": "application/json", "x-app-id": "citadel-agent"}
        if api_key:
            self._redactor.register_secret(api_key)
            headers["api-key"] = api_key
        if bearer_token:
            self._redactor.register_secret(bearer_token)
            headers["Authorization"] = f"Bearer {bearer_token}"
        return headers

    def call(
        self,
        method: str,
        path: str,
        *,
        api_key: str | None = None,
        bearer_token: str | None = None,
        json_body: Any = None,
    ) -> tuple[GatewayResponseInfo, Any]:
        """Send a request to the gateway and return diagnostics plus the decoded body."""
        started = time.perf_counter()
        response = self._http.request(
            method,
            f"{self.gateway_url}/{path.lstrip('/')}",
            headers=self._headers(api_key, bearer_token),
            json=json_body,
        )
        info = response_info(response, (time.perf_counter() - started) * 1000)
        return info, _decode(response)

    def version(self) -> dict[str, str]:
        """Return the deployed release tracks (``GET /version``, anonymous)."""
        info, body = self.call("GET", "/version")
        if not info.ok or not isinstance(body, Mapping):
            raise GatewayRequestError("GET /version", info)
        return {str(k): str(v) for k, v in body.items()}

    def backend_contract(self) -> BackendContractView:
        """Return the live backend routing contract (``GET /version/backend-contract``, anonymous)."""
        info, body = self.call("GET", "/version/backend-contract")
        if not info.ok or not isinstance(body, Mapping):
            raise GatewayRequestError("GET /version/backend-contract", info)
        return BackendContractView(body)

    def list_models(self, api_key: str) -> list[ModelInfo]:
        """Return models and aliases visible to a subscription (``GET /unified-ai/deployments``)."""
        info, body = self.call("GET", f"{UNIFIED_AI_PREFIX}/deployments", api_key=api_key)
        if not info.ok:
            raise GatewayRequestError("GET /unified-ai/deployments", info)
        return parse_deployments(body)

    def chat_smoke_test(
        self, api_key: str, model: str, *, bearer_token: str | None = None
    ) -> SmokeTestResult:
        """Send a minimal chat completion through the OpenAI-compatible Unified AI surface."""
        body = {"model": model, "messages": [{"role": "user", "content": SMOKE_TEST_PROMPT}]}
        info, payload = self.call(
            "POST",
            f"{UNIFIED_AI_PREFIX}/v1/chat/completions",
            api_key=api_key,
            bearer_token=bearer_token,
            json_body=body,
        )
        preview = None
        usage = None
        if info.ok and isinstance(payload, Mapping):
            usage = payload.get("usage")
            choices = payload.get("choices") or [{}]
            content = (choices[0].get("message") or {}).get("content")
            preview = content[:80] if isinstance(content, str) else None
        return SmokeTestResult(
            operation="chat.completions", model=model, info=info, usage=usage, preview=preview
        )

    def embeddings_smoke_test(
        self, api_key: str, model: str, *, bearer_token: str | None = None
    ) -> SmokeTestResult:
        """Send a minimal embeddings request through the OpenAI-compatible Unified AI surface."""
        info, payload = self.call(
            "POST",
            f"{UNIFIED_AI_PREFIX}/v1/embeddings",
            api_key=api_key,
            bearer_token=bearer_token,
            json_body={"model": model, "input": "ping"},
        )
        usage = payload.get("usage") if info.ok and isinstance(payload, Mapping) else None
        return SmokeTestResult(operation="embeddings", model=model, info=info, usage=usage)

    def close(self) -> None:
        self._http.close()


class GatewayRequestError(RuntimeError):
    """Raised when a gateway call that must succeed returns an error."""

    def __init__(self, operation: str, info: GatewayResponseInfo) -> None:
        detail = info.error.message if info.error else ""
        code = f" ({info.error.code})" if info.error and info.error.code else ""
        super().__init__(f"{operation} returned {info.status_code}{code}: {detail}".strip())
        self.info = info
