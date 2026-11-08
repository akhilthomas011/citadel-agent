# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Server-side portal role, resource-scope and sensitive-data authorization."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from citadel_agent.contracts.common import CitadelModel, GuidStr
from citadel_agent.portal.auth import PortalIdentity
from citadel_agent.portal.contracts import HubScope, PortalRole


class PortalAction(StrEnum):
    ASSISTANT_USE = "assistant.use"
    GATEWAY_READ = "gateway.read"
    OPERATIONS_READ = "operations.read"
    FINOPS_READ = "finops.read"
    FINOPS_CUSTOM_READ = "finops.custom-dimensions.read"
    RAW_LOGS_READ = "raw-logs.read"
    CONVERSATIONS_READ = "conversations.read"
    EXPORT_CREATE = "export.create"
    CHANGE_DRAFT = "change.draft"
    CHANGE_APPROVE = "change.approve"


class PortalGrantPermission(StrEnum):
    FINOPS_DATA = "finops-data"
    CUSTOM_DIMENSIONS = "custom-dimensions"
    RAW_LOGS = "raw-logs"
    CONVERSATION_HISTORY = "conversation-history"
    EXPORTS = "exports"


class HubGrant(CitadelModel):
    """A server-loaded grant for one user and one exact hub/environment scope."""

    subject_id: GuidStr
    scope: HubScope
    permissions: frozenset[PortalGrantPermission] = Field(default_factory=frozenset)
    enabled: bool = True


class PortalAuthorizationError(PermissionError):
    """The authenticated user lacks a role, scoped grant or sensitive-data grant."""


_ACTION_ROLES: dict[PortalAction, frozenset[PortalRole]] = {
    PortalAction.ASSISTANT_USE: frozenset(PortalRole),
    PortalAction.GATEWAY_READ: frozenset({PortalRole.VIEWER, PortalRole.OPERATOR, PortalRole.APPROVER}),
    PortalAction.OPERATIONS_READ: frozenset({PortalRole.VIEWER, PortalRole.OPERATOR, PortalRole.APPROVER}),
    PortalAction.FINOPS_READ: frozenset({PortalRole.FINOPS_ANALYST}),
    PortalAction.FINOPS_CUSTOM_READ: frozenset({PortalRole.FINOPS_ANALYST}),
    PortalAction.RAW_LOGS_READ: frozenset({PortalRole.VIEWER, PortalRole.OPERATOR, PortalRole.APPROVER}),
    PortalAction.CONVERSATIONS_READ: frozenset(PortalRole),
    PortalAction.EXPORT_CREATE: frozenset({PortalRole.FINOPS_ANALYST}),
    PortalAction.CHANGE_DRAFT: frozenset({PortalRole.OPERATOR}),
    PortalAction.CHANGE_APPROVE: frozenset({PortalRole.APPROVER}),
}

_ACTION_GRANTS: dict[PortalAction, PortalGrantPermission] = {
    PortalAction.FINOPS_READ: PortalGrantPermission.FINOPS_DATA,
    PortalAction.FINOPS_CUSTOM_READ: PortalGrantPermission.CUSTOM_DIMENSIONS,
    PortalAction.RAW_LOGS_READ: PortalGrantPermission.RAW_LOGS,
    PortalAction.CONVERSATIONS_READ: PortalGrantPermission.CONVERSATION_HISTORY,
    PortalAction.EXPORT_CREATE: PortalGrantPermission.EXPORTS,
}


def authorize_portal_action(
    identity: PortalIdentity,
    grant: HubGrant,
    requested_scope: HubScope,
    action: PortalAction,
) -> None:
    """Raise unless identity, exact hub scope, role and any sensitive grant all match."""
    if (
        not grant.enabled
        or identity.subject_id != grant.subject_id
        or identity.tenant_id != requested_scope.tenant_id
        or grant.scope != requested_scope
    ):
        raise PortalAuthorizationError("The user is not granted access to this hub scope.")
    if not identity.roles.intersection(_ACTION_ROLES[action]):
        raise PortalAuthorizationError(f"The user lacks a role for '{action.value}'.")
    required_grant = _ACTION_GRANTS.get(action)
    if required_grant is not None and required_grant not in grant.permissions:
        raise PortalAuthorizationError(f"The user lacks the '{required_grant.value}' grant.")
