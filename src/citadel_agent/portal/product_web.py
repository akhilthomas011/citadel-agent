# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Same-origin browser routes and static SPA hosting, with no browser-held Azure tokens."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path as FilePath
from typing import TYPE_CHECKING, Annotated, Any, Protocol

import httpx
from azure.core.exceptions import AzureError
from fastapi import APIRouter, FastAPI, HTTPException, Path, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from citadel_agent.portal.auth_web import PortalAuthConfig, _require_csrf, resolve_browser_session
from citadel_agent.portal.product_contracts import (
    ChangeDecision,
    DashboardQuery,
    OperationsQuery,
    ReportStartRequest,
    RequestEvidenceQuery,
    TurnRequest,
)
from citadel_agent.portal.sessions import PortalSessionStore

if TYPE_CHECKING:
    from citadel_agent.portal.bff import AgentTokenProvider

ConversationId = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]
ChangeId = Annotated[str, Path(pattern=r"^cs-\d{8}-\d{6}-[0-9a-f]{6}$")]
ReportId = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]


class AgentProductClient(Protocol):
    async def request_json(
        self,
        method: str,
        path: str,
        *,
        token: str,
        session_reference: str,
        body: dict[str, Any] | None = None,
    ) -> Any: ...


def create_product_web_router(
    *,
    auth_config: PortalAuthConfig,
    session_store: PortalSessionStore,
    agent_token_provider: AgentTokenProvider,
    agent_client: AgentProductClient,
    clock: Callable[[], datetime],
) -> APIRouter:
    from citadel_agent.portal.bff import AgentApiError

    router = APIRouter(prefix="/api", tags=["portal"])

    async def relay(request: Request, response: Response, method: str, path: str, body=None):
        resolved = await run_in_threadpool(resolve_browser_session, request, session_store, clock)
        if resolved is None:
            raise HTTPException(status_code=401, detail="Authentication is required.")
        reference, session = resolved
        if method != "GET":
            _require_csrf(request, session, auth_config)
        response.headers["Cache-Control"] = "no-store"
        try:
            token = await run_in_threadpool(agent_token_provider.get_token)
            return await agent_client.request_json(
                method,
                path,
                token=token,
                session_reference=reference,
                body=body.model_dump(by_alias=True, mode="json") if body is not None else None,
            )
        except AgentApiError as exc:
            status = exc.status_code if exc.status_code in {400, 403, 404, 409, 422} else 503
            messages = {
                400: "Request is invalid. Do not send secrets in chat; use Key Vault references.",
                403: "Your roles or hub grants do not permit this action.",
                404: "The requested record is unavailable.",
                409: "Request conflicts with current state. Reload before retrying or submitting a change.",
                422: "Request fields or time window are invalid.",
                503: "The private agent or its data source is unavailable. Retry shortly.",
            }
            raise HTTPException(status_code=status, detail=messages[status]) from exc
        except (AzureError, httpx.RequestError) as exc:
            raise HTTPException(status_code=503, detail="The private agent service is unavailable.") from exc

    @router.get("/portal")
    async def metadata(request: Request, response: Response):
        return await relay(request, response, "GET", "/internal/portal")

    @router.get("/inventory")
    async def inventory(request: Request, response: Response):
        return await relay(request, response, "GET", "/internal/inventory")

    @router.post("/finops/query")
    async def finops(query: DashboardQuery, request: Request, response: Response):
        return await relay(request, response, "POST", "/internal/finops/query", query)

    @router.post("/finops/export")
    async def export_finops(query: DashboardQuery, request: Request, response: Response):
        return await relay(request, response, "POST", "/internal/finops/export", query)

    @router.post("/finops/reports", status_code=202)
    async def start_report(body: ReportStartRequest, request: Request, response: Response):
        return await relay(request, response, "POST", "/internal/finops/reports", body)

    @router.get("/finops/reports")
    async def recent_reports(request: Request, response: Response):
        return await relay(request, response, "GET", "/internal/finops/reports")

    @router.get("/finops/reports/{report_id}")
    async def get_report(report_id: ReportId, request: Request, response: Response):
        return await relay(request, response, "GET", f"/internal/finops/reports/{report_id}")

    @router.post("/finops/reports/{report_id}/advance")
    async def advance_report(report_id: ReportId, request: Request, response: Response):
        return await relay(request, response, "POST", f"/internal/finops/reports/{report_id}/advance")

    @router.post("/finops/reports/{report_id}/cancel")
    async def cancel_report(report_id: ReportId, request: Request, response: Response):
        return await relay(request, response, "POST", f"/internal/finops/reports/{report_id}/cancel")

    @router.post("/finops/reports/{report_id}/export")
    async def export_report(report_id: ReportId, request: Request, response: Response):
        return await relay(request, response, "POST", f"/internal/finops/reports/{report_id}/export")

    @router.post("/operations/query")
    async def operations(query: OperationsQuery, request: Request, response: Response):
        return await relay(request, response, "POST", "/internal/operations/query", query)

    @router.post("/operations/evidence")
    async def evidence(body: RequestEvidenceQuery, request: Request, response: Response):
        return await relay(request, response, "POST", "/internal/operations/evidence", body)

    @router.post("/operations/alerts")
    async def alerts(body: DashboardQuery, request: Request, response: Response):
        return await relay(request, response, "POST", "/internal/operations/alerts", body)

    @router.post("/operations/pipeline")
    async def pipeline(body: DashboardQuery, request: Request, response: Response):
        return await relay(request, response, "POST", "/internal/operations/pipeline", body)

    @router.post("/operations/attribution")
    async def attribution(body: DashboardQuery, request: Request, response: Response):
        return await relay(request, response, "POST", "/internal/operations/attribution", body)

    @router.get("/operations/compatibility")
    async def compatibility(request: Request, response: Response):
        return await relay(request, response, "GET", "/internal/operations/compatibility")

    @router.get("/conversations")
    async def conversations(request: Request, response: Response):
        return await relay(request, response, "GET", "/internal/conversations")

    @router.post("/conversations", status_code=201)
    async def create_conversation(request: Request, response: Response):
        return await relay(request, response, "POST", "/internal/conversations")

    @router.get("/conversations/{conversation_id}")
    async def conversation(conversation_id: ConversationId, request: Request, response: Response):
        return await relay(request, response, "GET", f"/internal/conversations/{conversation_id}")

    @router.post("/conversations/{conversation_id}/cancel")
    async def cancel_turn(conversation_id: ConversationId, request: Request, response: Response):
        return await relay(request, response, "POST", f"/internal/conversations/{conversation_id}/cancel")

    @router.post("/conversations/{conversation_id}/turns", status_code=202)
    async def turn(conversation_id: ConversationId, body: TurnRequest, request: Request, response: Response):
        return await relay(
            request, response, "POST", f"/internal/conversations/{conversation_id}/turns", body
        )

    @router.get("/changes")
    async def changes(request: Request, response: Response):
        return await relay(request, response, "GET", "/internal/changes")

    @router.post("/changes/{change_id}/approve")
    async def approve(change_id: ChangeId, body: ChangeDecision, request: Request, response: Response):
        return await relay(request, response, "POST", f"/internal/changes/{change_id}/approve", body)

    @router.post("/changes/{change_id}/execute")
    async def execute(change_id: ChangeId, body: ChangeDecision, request: Request, response: Response):
        return await relay(request, response, "POST", f"/internal/changes/{change_id}/execute", body)

    return router


def mount_portal_ui(app: FastAPI, directory: FilePath) -> None:
    if (directory / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=directory / "assets"), name="portal-assets")

    @app.get("/", include_in_schema=False)
    @app.get("/assistant", include_in_schema=False)
    @app.get("/finops", include_in_schema=False)
    @app.get("/operations", include_in_schema=False)
    def index() -> FileResponse:
        path = directory / "index.html"
        if not path.is_file():
            raise HTTPException(status_code=503, detail="Portal UI is not built. Run the frontend build.")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith(("/api/", "/auth/")):
            response.headers["Cache-Control"] = "no-store"
        return response
