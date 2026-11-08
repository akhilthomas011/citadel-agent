# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Browser-facing BFF routes that keep user sessions and agent credentials server-side."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path as FilePath
from typing import TYPE_CHECKING, Annotated, Any, Protocol
from urllib.parse import quote

import httpx
from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError
from fastapi import FastAPI, HTTPException, Path, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from citadel_agent.portal.api import PortalJobStatusResponse
from citadel_agent.portal.auth import EntraTokenValidator
from citadel_agent.portal.auth_web import (
    OAuthClient,
    PortalAuthConfig,
    create_auth_router,
    resolve_browser_session,
)
from citadel_agent.portal.sessions import PortalSessionStore

if TYPE_CHECKING:
    from citadel_agent.portal.product_web import AgentProductClient


class AgentTokenProvider(Protocol):
    def get_token(self) -> str: ...


class AgentJobStatusClient(Protocol):
    async def get_job_status(
        self,
        *,
        token: str,
        session_reference: str,
        job_id: str,
    ) -> PortalJobStatusResponse: ...


class AgentApiError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"Private agent API returned HTTP {status_code}.")
        self.status_code = status_code


class ManagedIdentityAgentTokenProvider:
    """Acquire the private agent API app token using the host's managed identity."""

    def __init__(self, credential: TokenCredential, audience: str, *, token_scope: str | None = None) -> None:
        if not audience.strip():
            raise ValueError("agent API audience must not be empty")
        self._credential = credential
        self._scope = token_scope or f"{audience.rstrip('/')}/.default"
        if not self._scope.endswith("/.default") or any(char.isspace() for char in self._scope):
            raise ValueError("agent token scope must be a single application /.default scope")

    def get_token(self) -> str:
        token = self._credential.get_token(self._scope)
        if not token.token:
            raise RuntimeError("Managed identity returned an empty agent API token.")
        return token.token


class HttpxAgentJobStatusClient:
    """Typed HTTP adapter for the private agent job-status endpoint."""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("private agent API URL must use HTTPS")
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        token: str,
        session_reference: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        response = await self._client.request(
            method,
            f"{self._base_url}{path}",
            headers={"Authorization": f"Bearer {token}", "X-Portal-Session-Ref": session_reference},
            json=body,
        )
        if response.status_code not in {200, 201, 202}:
            raise AgentApiError(response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise AgentApiError(502) from exc

    async def get_job_status(
        self,
        *,
        token: str,
        session_reference: str,
        job_id: str,
    ) -> PortalJobStatusResponse:
        response = await self._client.get(
            f"{self._base_url}/internal/jobs/{quote(job_id, safe='')}",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Portal-Session-Ref": session_reference,
            },
        )
        if response.status_code != 200:
            raise AgentApiError(response.status_code)
        try:
            return PortalJobStatusResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise AgentApiError(502) from exc


def create_bff_app(
    *,
    auth_config: PortalAuthConfig,
    oauth_client: OAuthClient,
    delegated_token_validator: EntraTokenValidator,
    session_store: PortalSessionStore,
    agent_token_provider: AgentTokenProvider,
    agent_client: AgentJobStatusClient,
    readiness_probe: Callable[[], None] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    product_client: AgentProductClient | None = None,
    static_directory: FilePath | None = None,
) -> FastAPI:
    """Compose the browser BFF; it never accepts user identity from request headers."""
    app = FastAPI(title="Citadel portal BFF", version="1")
    app.include_router(
        create_auth_router(
            config=auth_config,
            oauth_client=oauth_client,
            token_validator=delegated_token_validator,
            store=session_store,
            clock=clock,
        )
    )

    @app.get("/health/live", include_in_schema=False)
    def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", include_in_schema=False)
    def ready() -> dict[str, str]:
        if readiness_probe is None:
            raise HTTPException(status_code=503, detail="Readiness checks are not configured.")
        try:
            readiness_probe()
        except AzureError as exc:
            raise HTTPException(status_code=503, detail="Portal state store is unavailable.") from exc
        return {"status": "ready"}

    @app.get("/api/jobs/{job_id}", response_model=PortalJobStatusResponse)
    async def get_job_status(
        job_id: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
        request: Request,
        response: Response,
    ) -> PortalJobStatusResponse:
        resolved = await run_in_threadpool(resolve_browser_session, request, session_store, clock)
        if resolved is None:
            raise HTTPException(status_code=401, detail="Authentication is required.")
        session_reference, _session = resolved

        try:
            token = await run_in_threadpool(agent_token_provider.get_token)
        except AzureError as exc:
            raise HTTPException(status_code=503, detail="Private agent service is unavailable.") from exc

        try:
            result = await agent_client.get_job_status(
                token=token,
                session_reference=session_reference,
                job_id=job_id,
            )
        except AgentApiError as exc:
            if exc.status_code in (403, 404):
                raise HTTPException(status_code=exc.status_code, detail="Job is unavailable.") from exc
            if exc.status_code == 401:
                raise HTTPException(
                    status_code=503,
                    detail="Private agent service authentication failed.",
                ) from exc
            raise HTTPException(status_code=503, detail="Private agent service is unavailable.") from exc
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail="Private agent service is unavailable.") from exc

        response.headers["Cache-Control"] = "no-store"
        return result

    if product_client is not None:
        from citadel_agent.portal.product_api import register_product_errors
        from citadel_agent.portal.product_web import create_product_web_router, mount_portal_ui

        app.include_router(
            create_product_web_router(
                auth_config=auth_config,
                session_store=session_store,
                agent_token_provider=agent_token_provider,
                agent_client=product_client,
                clock=clock,
            )
        )
        register_product_errors(app)
        mount_portal_ui(app, static_directory or FilePath(__file__).parent / "static")
    return app
