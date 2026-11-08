# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Authenticated product endpoints. Every operation rechecks the exact hub grant."""

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from azure.core.exceptions import AzureError
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Path, Request
from starlette.concurrency import run_in_threadpool

from citadel_agent.analytics.read_model import AnalyticsReader
from citadel_agent.analytics.snapshots import AnalyticsUnavailable
from citadel_agent.azure_clients.monitor import AlertReader
from citadel_agent.changes.workspace import WorkspaceError
from citadel_agent.portal.agent_auth import AgentCallContext
from citadel_agent.portal.api import HubGrantReader
from citadel_agent.portal.assistant import AssistantUnavailable, PortalAssistantService
from citadel_agent.portal.authz import (
    HubGrant,
    PortalAction,
    PortalAuthorizationError,
    authorize_portal_action,
)
from citadel_agent.portal.contracts import InventorySnapshot
from citadel_agent.portal.data import PortalDataService
from citadel_agent.portal.operations import OperationsService
from citadel_agent.portal.product_contracts import (
    AlertInstancesResponse,
    ChangeDecision,
    ChangeResponse,
    ConversationResponse,
    DashboardQuery,
    FinOpsReportResponse,
    FinOpsResponse,
    OperationsAttribution,
    OperationsQuery,
    OperationsResponse,
    PipelineHealthResponse,
    PortalHubConfig,
    ReportStartRequest,
    RequestEvidenceQuery,
    RequestEvidenceResponse,
    TurnRequest,
)
from citadel_agent.portal.report_export import finops_csv
from citadel_agent.portal.reports import FinOpsReportService, ReportUnavailable
from citadel_agent.portal.sessions import PortalStateConflict
from citadel_agent.portal.state import ProductStore
from citadel_agent.proc import CommandError

logger = logging.getLogger(__name__)
ConversationId = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]
ChangeId = Annotated[str, Path(pattern=r"^cs-\d{8}-\d{6}-[0-9a-f]{6}$")]
ReportId = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]


@dataclass(frozen=True)
class PortalProduct:
    config: PortalHubConfig
    grant_store: HubGrantReader
    data: PortalDataService
    assistant: PortalAssistantService
    analytics: AnalyticsReader | None = None
    alert_reader: AlertReader | None = None

    @property
    def operations(self) -> OperationsService:
        return OperationsService(self.data, analytics=self.analytics, alerts=self.alert_reader)

    @property
    def reports(self) -> FinOpsReportService:
        return FinOpsReportService(
            ProductStore(self.assistant.store.container, self.config.scope, retention_days=1),
            self.data,
            analytics=self.analytics,
        )

    def authorize(self, context: AgentCallContext, action: PortalAction) -> HubGrant:
        current = self.grant_store.get(context.user.subject_id, self.config.scope)
        if current is None:
            raise PortalAuthorizationError("Access to this hub scope is not granted.")
        authorize_portal_action(context.user, current.grant, self.config.scope, action)
        return current.grant


def create_product_router(product: PortalProduct, resolve_context: Callable) -> APIRouter:
    router = APIRouter(prefix="/internal", tags=["portal"])
    reports = product.reports

    def authorized(context: AgentCallContext, action: PortalAction) -> HubGrant:
        try:
            return product.authorize(context, action)
        except PortalAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def custom_access(context: AgentCallContext) -> bool:
        if not product.config.custom_dimension_labels:
            return False
        try:
            product.authorize(context, PortalAction.FINOPS_CUSTOM_READ)
        except PortalAuthorizationError:
            return False
        return True

    def financial_query(context: AgentCallContext, query: DashboardQuery) -> None:
        authorized(context, PortalAction.FINOPS_READ)
        if query.uses_custom_dimensions():
            authorized(context, PortalAction.FINOPS_CUSTOM_READ)
            names = set(query.dimension_filters) | {query.group_by}
            from citadel_agent.analytics.views import CUSTOM_DIMENSIONS

            if any(
                name in CUSTOM_DIMENSIONS and name not in product.config.custom_dimension_labels
                for name in names
            ):
                raise HTTPException(status_code=403, detail="Custom attribution is not configured.")

    def financial_report(context: AgentCallContext, report_id: str) -> None:
        authorized(context, PortalAction.FINOPS_READ)
        if reports.requires_custom(report_id, context.user.subject_id) and not custom_access(context):
            raise HTTPException(
                status_code=403, detail="Custom attribution requires its configured sensitive-data grant."
            )

    @router.get("/portal")
    def metadata(context: Annotated[AgentCallContext, Depends(resolve_context)]) -> dict[str, object]:
        authorized(context, PortalAction.ASSISTANT_USE)
        actions: list[str] = []
        for action in PortalAction:
            if action is PortalAction.FINOPS_CUSTOM_READ and not product.config.custom_dimension_labels:
                continue
            try:
                product.authorize(context, action)
            except PortalAuthorizationError:
                continue
            actions.append(action.value)
        return {
            "scope": product.config.scope.model_dump(by_alias=True, mode="json"),
            "displayName": product.config.display_name,
            "allowedActions": actions,
            "changeMode": "pull-request",
            "analyticsEnabled": product.analytics is not None,
            "assistantExecutionMode": "durable"
            if product.assistant.durable_turns
            else "in-process-compatibility",
            "customDimensionLabels": product.config.custom_dimension_labels if custom_access(context) else {},
        }

    @router.get("/inventory", response_model=InventorySnapshot)
    def inventory(context: Annotated[AgentCallContext, Depends(resolve_context)]) -> InventorySnapshot:
        authorized(context, PortalAction.GATEWAY_READ)
        return product.data.inventory()

    @router.post("/finops/query", response_model=FinOpsResponse)
    def finops(
        query: DashboardQuery, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> FinOpsResponse:
        financial_query(context, query)
        return product.data.finops(query)

    @router.post("/finops/export")
    def export_finops(
        query: DashboardQuery, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> dict[str, str]:
        financial_query(context, query)
        authorized(context, PortalAction.EXPORT_CREATE)
        report = product.data.finops(query)
        product.assistant.store.create(
            "portal-audit",
            secrets.token_hex(32),
            context.user.subject_id,
            {"event": "finops.export", "window": query.window.model_dump(mode="json")},
        )
        return finops_csv(query, report)

    @router.post("/finops/reports", response_model=FinOpsReportResponse, status_code=202)
    def start_report(
        body: ReportStartRequest, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> FinOpsReportResponse:
        authorized(context, PortalAction.FINOPS_READ)
        return reports.start(body, context.user.subject_id, custom_access=custom_access(context))

    @router.get("/finops/reports", response_model=list[FinOpsReportResponse])
    def recent_reports(
        context: Annotated[AgentCallContext, Depends(resolve_context)],
    ) -> list[FinOpsReportResponse]:
        authorized(context, PortalAction.FINOPS_READ)
        return reports.recent(context.user.subject_id, custom_access=custom_access(context))

    @router.get("/finops/reports/{report_id}", response_model=FinOpsReportResponse)
    def get_report(
        report_id: ReportId, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> FinOpsReportResponse:
        financial_report(context, report_id)
        return reports.get(report_id, context.user.subject_id)

    @router.post("/finops/reports/{report_id}/advance", response_model=FinOpsReportResponse)
    def advance_report(
        report_id: ReportId, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> FinOpsReportResponse:
        financial_report(context, report_id)
        return reports.advance(report_id, context.user.subject_id)

    @router.post("/finops/reports/{report_id}/cancel", response_model=FinOpsReportResponse)
    def cancel_report(
        report_id: ReportId, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> FinOpsReportResponse:
        financial_report(context, report_id)
        return reports.cancel(report_id, context.user.subject_id)

    @router.post("/finops/reports/{report_id}/export")
    def export_report(
        report_id: ReportId, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> dict[str, str]:
        financial_report(context, report_id)
        authorized(context, PortalAction.EXPORT_CREATE)
        query, report = reports.completed(report_id, context.user.subject_id)
        product.assistant.store.create(
            "portal-audit",
            secrets.token_hex(32),
            context.user.subject_id,
            {
                "event": "finops.report.export",
                "reportId": report_id,
                "window": query.window.model_dump(mode="json"),
            },
        )
        return finops_csv(query, report)

    @router.post("/operations/query", response_model=OperationsResponse)
    def operations(
        query: OperationsQuery, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> OperationsResponse:
        authorized(context, PortalAction.OPERATIONS_READ)
        return product.data.operations(query)

    @router.post("/operations/evidence", response_model=RequestEvidenceResponse)
    def request_evidence(
        query: RequestEvidenceQuery, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> RequestEvidenceResponse:
        authorized(context, PortalAction.OPERATIONS_READ)
        authorized(context, PortalAction.RAW_LOGS_READ)
        result = product.operations.evidence(query)
        product.assistant.store.create(
            "portal-audit",
            secrets.token_hex(32),
            context.user.subject_id,
            {"event": "operations.request.lookup", "correlationId": str(query.correlation_id)},
        )
        return result

    @router.post("/operations/alerts", response_model=AlertInstancesResponse)
    def alert_instances(
        query: DashboardQuery, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> AlertInstancesResponse:
        authorized(context, PortalAction.OPERATIONS_READ)
        return product.operations.alert_instances(query)

    @router.post("/operations/pipeline", response_model=PipelineHealthResponse)
    def pipeline_health(
        query: DashboardQuery, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> PipelineHealthResponse:
        authorized(context, PortalAction.OPERATIONS_READ)
        return product.operations.pipeline_health(query)

    @router.post("/operations/attribution", response_model=OperationsAttribution)
    def operational_attribution(
        query: DashboardQuery, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> OperationsAttribution:
        authorized(context, PortalAction.OPERATIONS_READ)
        return product.operations.attribution(query)

    @router.get("/operations/compatibility")
    def supported_compatibility(
        context: Annotated[AgentCallContext, Depends(resolve_context)],
    ) -> dict[str, object]:
        authorized(context, PortalAction.OPERATIONS_READ)
        return product.operations.compatibility()

    @router.get("/conversations", response_model=list[ConversationResponse])
    def conversations(
        context: Annotated[AgentCallContext, Depends(resolve_context)],
    ) -> list[ConversationResponse]:
        authorized(context, PortalAction.ASSISTANT_USE)
        return product.assistant.conversations(context.user)

    @router.post("/conversations", response_model=ConversationResponse, status_code=201)
    def create_conversation(
        context: Annotated[AgentCallContext, Depends(resolve_context)],
    ) -> ConversationResponse:
        authorized(context, PortalAction.ASSISTANT_USE)
        return product.assistant.create_conversation(context.user)

    @router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
    def conversation(
        conversation_id: ConversationId, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> ConversationResponse:
        authorized(context, PortalAction.ASSISTANT_USE)
        return product.assistant.conversation(conversation_id, context.user)

    @router.post(
        "/conversations/{conversation_id}/turns", response_model=ConversationResponse, status_code=202
    )
    async def turn(
        conversation_id: ConversationId,
        request: TurnRequest,
        background_tasks: BackgroundTasks,
        context: Annotated[AgentCallContext, Depends(resolve_context)],
    ) -> ConversationResponse:
        grant = authorized(context, PortalAction.ASSISTANT_USE)
        return await product.assistant.turn(
            conversation_id,
            request,
            context.user,
            grant,
            background_tasks=background_tasks,
            session_reference=context.session.id if product.assistant.durable_turns else None,
        )

    @router.post("/conversations/{conversation_id}/cancel", response_model=ConversationResponse)
    def cancel_turn(
        conversation_id: ConversationId, context: Annotated[AgentCallContext, Depends(resolve_context)]
    ) -> ConversationResponse:
        authorized(context, PortalAction.ASSISTANT_USE)
        return product.assistant.cancel_turn(conversation_id, context.user)

    @router.get("/changes", response_model=list[ChangeResponse])
    def changes(context: Annotated[AgentCallContext, Depends(resolve_context)]) -> list[ChangeResponse]:
        try:
            product.authorize(context, PortalAction.CHANGE_DRAFT)
        except PortalAuthorizationError:
            authorized(context, PortalAction.CHANGE_APPROVE)
        return product.assistant.changes()

    @router.post("/changes/{change_id}/approve", response_model=ChangeResponse)
    def approve(
        change_id: ChangeId,
        decision: ChangeDecision,
        context: Annotated[AgentCallContext, Depends(resolve_context)],
    ) -> ChangeResponse:
        grant = authorized(context, PortalAction.CHANGE_APPROVE)
        return product.assistant.approve(change_id, decision, context.user, grant)

    @router.post("/changes/{change_id}/execute", response_model=ChangeResponse)
    async def execute(
        change_id: ChangeId,
        decision: ChangeDecision,
        context: Annotated[AgentCallContext, Depends(resolve_context)],
    ) -> ChangeResponse:
        grant = authorized(context, PortalAction.CHANGE_DRAFT)
        return await run_in_threadpool(product.assistant.execute, change_id, decision, context.user, grant)

    return router


def register_product_errors(app: FastAPI) -> None:
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    @app.exception_handler(RequestValidationError)
    async def validation(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "Request fields or time window are invalid."})

    @app.exception_handler(PortalStateConflict)
    async def conflict(_request: Request, exc: PortalStateConflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def not_found(_request: Request, _exc: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "The requested record is unavailable."})

    @app.exception_handler(PermissionError)
    async def forbidden(_request: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def invalid(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    async def unavailable(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("Portal dependency is unavailable (%s).", type(exc).__name__)
        return JSONResponse(
            status_code=503, content={"detail": "The requested service is temporarily unavailable."}
        )

    for exception in (
        AzureError,
        AnalyticsUnavailable,
        AssistantUnavailable,
        ReportUnavailable,
        WorkspaceError,
        CommandError,
    ):
        app.add_exception_handler(exception, unavailable)
