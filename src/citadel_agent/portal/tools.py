# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Hosted assistant tools that share the dashboard's scope and accounting semantics."""

import functools
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from citadel_agent.analytics.rollups import UsageDimension
from citadel_agent.portal.auth import PortalIdentity
from citadel_agent.portal.authz import (
    HubGrant,
    PortalAction,
    PortalAuthorizationError,
    authorize_portal_action,
)
from citadel_agent.portal.contracts import TimeRange
from citadel_agent.portal.data import PortalDataService
from citadel_agent.portal.product_contracts import DashboardQuery
from citadel_agent.safety.audit import AuditLog, AuditOutcome
from citadel_agent.tools.registry import ToolRegistry, ToolSpec

_TOOL_ACTIONS = {
    "search_docs": (PortalAction.ASSISTANT_USE,),
    "read_doc": (PortalAction.ASSISTANT_USE,),
    "gateway_overview": (PortalAction.GATEWAY_READ,),
    "list_models": (PortalAction.GATEWAY_READ,),
    "list_access_contracts": (PortalAction.GATEWAY_READ,),
    "describe_access_contract": (PortalAction.GATEWAY_READ,),
    "generate_client_code": (PortalAction.GATEWAY_READ,),
    "backend_contract_summary": (PortalAction.GATEWAY_READ,),
    "preflight": (PortalAction.GATEWAY_READ,),
    "gateway_logs": (PortalAction.RAW_LOGS_READ,),
    "explain_error": (PortalAction.RAW_LOGS_READ,),
    "usage_report": (PortalAction.FINOPS_READ,),
    "change_status": (PortalAction.CHANGE_DRAFT, PortalAction.CHANGE_APPROVE),
    "draft_access_contract": (PortalAction.CHANGE_DRAFT,),
    "draft_backend_change": (PortalAction.CHANGE_DRAFT,),
    "draft_publish_contract": (PortalAction.CHANGE_DRAFT,),
    "draft_alert_rules": (PortalAction.CHANGE_DRAFT,),
    "start_key_rotation": (PortalAction.CHANGE_DRAFT,),
    "advance_key_rotation": (PortalAction.CHANGE_DRAFT,),
    "prepare_change": (PortalAction.CHANGE_DRAFT,),
}


def hosted_registry(
    registry: ToolRegistry,
    identity: PortalIdentity,
    grant: HubGrant,
    *,
    data: PortalDataService | None,
    audit: AuditLog,
    refresh_authorization: Callable[[], tuple[PortalIdentity, HubGrant]] | None = None,
) -> ToolRegistry:
    selected = ToolRegistry()
    for name, original in registry.specs.items():
        actions = _TOOL_ACTIONS.get(name, ())
        try:
            _authorize(identity, grant, actions)
        except PortalAuthorizationError:
            continue
        if name == "usage_report":
            if data is None:
                continue
            original = hosted_usage_tool(data)
        selected.add(
            replace(
                original,
                function=_guard_tool(original, identity, grant, actions, audit, refresh_authorization),
            )
        )
    return selected


def _authorize(identity: PortalIdentity, grant: HubGrant, actions: tuple[PortalAction, ...]) -> None:
    for action in actions:
        try:
            authorize_portal_action(identity, grant, grant.scope, action)
        except PortalAuthorizationError:
            continue
        return
    raise PortalAuthorizationError("This tool is not authorized for the portal user.")


def _guard_tool(
    spec: ToolSpec,
    identity: PortalIdentity,
    grant: HubGrant,
    actions: tuple[PortalAction, ...],
    audit: AuditLog,
    refresh_authorization: Callable[[], tuple[PortalIdentity, HubGrant]] | None = None,
) -> Callable[..., Any]:
    @functools.wraps(spec.function)
    def invoke(*args: Any, **kwargs: Any) -> Any:
        current_identity, current_grant = (
            refresh_authorization() if refresh_authorization else (identity, grant)
        )
        _authorize(current_identity, current_grant, actions)
        audit.record("portal.tool.started", tool=spec.name, actor=identity.subject_id)
        outcome = AuditOutcome.ERROR
        try:
            result = spec.function(*args, **kwargs)
            outcome = AuditOutcome.OK
            return result
        finally:
            audit.record("portal.tool.completed", tool=spec.name, actor=identity.subject_id, outcome=outcome)

    return invoke


def hosted_usage_tool(data: PortalDataService) -> ToolSpec:
    def usage_report(
        days: Annotated[int, Field(ge=1, le=90, description="Number of UTC days to report.")] = 7,
        group_by: Literal["model", "deployment", "product", "application", "backend", "region"] = "model",
    ) -> str:
        now = datetime.now(UTC)
        report = data.finops(
            DashboardQuery(
                window=TimeRange(start=now - timedelta(days=days), end=now),
                group_by=UsageDimension(group_by),
            )
        )
        return report.model_dump_json(by_alias=True, exclude={"aggregates"}) + (
            "\nUse the FinOps page to inspect daily allocation and attribution. "
            "Amounts are usage estimates for priced observations, not complete invoices. "
            "Explain all data-quality, unpriced-usage and scan-limit warnings."
        )

    return ToolSpec(
        "usage_report",
        usage_report,
        "Scoped observed usage, effective-dated estimated costs, pricing coverage and data quality "
        "using the same source and accounting rules as the FinOps dashboard.",
    )
