# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Private agent API composition and scope-authorized portal endpoints."""

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Protocol

from azure.core.exceptions import AzureError
from fastapi import Depends, FastAPI, HTTPException, Path
from pydantic import Field

from citadel_agent.contracts.common import CitadelModel
from citadel_agent.portal.agent_auth import AgentCallContext, create_agent_context_dependency
from citadel_agent.portal.auth import EntraServiceTokenValidator
from citadel_agent.portal.authz import (
    PortalAction,
    PortalAuthorizationError,
    authorize_portal_action,
)
from citadel_agent.portal.contracts import HubScope
from citadel_agent.portal.grants import VersionedHubGrant
from citadel_agent.portal.jobs import (
    PortalJobKind,
    PortalJobRecord,
    PortalJobStatus,
    PortalJobStore,
    VersionedPortalJob,
)
from citadel_agent.portal.sessions import PortalSessionStore

if TYPE_CHECKING:
    from citadel_agent.portal.product_api import PortalProduct


class HubGrantReader(Protocol):
    def get(self, subject_id: str, scope: HubScope) -> VersionedHubGrant | None: ...


class PortalJobStatusResponse(CitadelModel):
    """Safe, user-scoped job status; internal result references are not exposed."""

    job_id: str = Field(alias="jobId")
    job_kind: PortalJobKind = Field(alias="jobKind")
    scope: HubScope
    status: PortalJobStatus
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    started_at: datetime | None = Field(alias="startedAt")
    completed_at: datetime | None = Field(alias="completedAt")
    error_code: str | None = Field(alias="errorCode")


_JOB_ACTION: dict[PortalJobKind, PortalAction] = {
    PortalJobKind.ASSISTANT_TURN: PortalAction.ASSISTANT_USE,
    PortalJobKind.CHANGE_PREVIEW: PortalAction.CHANGE_DRAFT,
    PortalJobKind.CHANGE_EXECUTION: PortalAction.CHANGE_DRAFT,
    PortalJobKind.HUB_PROVISIONING: PortalAction.CHANGE_DRAFT,
    PortalJobKind.ANALYTICS_EXPORT: PortalAction.EXPORT_CREATE,
}


def create_agent_app(
    *,
    service_token_validator: EntraServiceTokenValidator,
    session_store: PortalSessionStore,
    grant_store: HubGrantReader,
    job_store: PortalJobStore,
    readiness_probe: Callable[[], None] | None = None,
    product: "PortalProduct | None" = None,
) -> FastAPI:
    """Build the private agent API around shared durable state and trusted Entra validation."""
    app = FastAPI(title="Citadel private agent API", version="1")
    app.state.portal_product = product
    app.state.portal_sessions = session_store
    resolve_context = create_agent_context_dependency(
        service_token_validator=service_token_validator,
        session_store=session_store,
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

    @app.get("/internal/jobs/{job_id}", response_model=PortalJobStatusResponse)
    def get_job(
        job_id: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
        context: Annotated[AgentCallContext, Depends(resolve_context)],
    ) -> PortalJobStatusResponse:
        try:
            job: VersionedPortalJob = job_store.get(job_id)
            record = job.record
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job was not found.") from exc

        if record.subject_id != context.user.subject_id:
            raise HTTPException(status_code=404, detail="Job was not found.")
        versioned_grant = grant_store.get(context.user.subject_id, record.scope)
        if versioned_grant is None:
            raise HTTPException(status_code=403, detail="Access to this hub scope is not granted.")
        try:
            authorize_portal_action(
                context.user,
                versioned_grant.grant,
                record.scope,
                _JOB_ACTION[record.job_kind],
            )
        except PortalAuthorizationError as exc:
            raise HTTPException(status_code=403, detail="Access to this job is not authorized.") from exc

        return _job_status(record)

    if product is not None:
        from citadel_agent.portal.product_api import create_product_router, register_product_errors

        app.include_router(create_product_router(product, resolve_context))
        register_product_errors(app)
    return app


def _job_status(record: PortalJobRecord) -> PortalJobStatusResponse:
    return PortalJobStatusResponse(
        jobId=record.id,
        jobKind=record.job_kind,
        scope=record.scope,
        status=record.status,
        createdAt=record.created_at,
        updatedAt=record.updated_at,
        startedAt=record.started_at,
        completedAt=record.completed_at,
        errorCode=record.error_code,
    )
