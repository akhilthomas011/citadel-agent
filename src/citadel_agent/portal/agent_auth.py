# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Authenticate the BFF service and resolve its user through shared server-side session state."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException, Request

from citadel_agent.portal.auth import (
    EntraServiceTokenValidator,
    PortalAuthenticationError,
    PortalIdentity,
    PortalServiceIdentity,
)
from citadel_agent.portal.sessions import PortalSessionRecord, PortalSessionStore

_SESSION_REFERENCE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_HEADER = "X-Portal-Session-Ref"


@dataclass(frozen=True)
class AgentCallContext:
    """Verified BFF caller plus effective user loaded from durable portal session state."""

    service: PortalServiceIdentity
    session: PortalSessionRecord

    @property
    def user(self) -> PortalIdentity:
        return identity_from_session(self.session)


def identity_from_session(session: PortalSessionRecord) -> PortalIdentity:
    return PortalIdentity(
        subject_id=session.subject_id,
        tenant_id=session.tenant_id,
        client_id=session.client_id,
        expires_at=session.expires_at,
        scopes=session.scopes,
        roles=session.roles,
    )


def create_agent_context_dependency(
    *,
    service_token_validator: EntraServiceTokenValidator,
    session_store: PortalSessionStore,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Callable[[Request], AgentCallContext]:
    """Create a FastAPI dependency that rejects forged user headers and expired sessions."""

    def resolve(request: Request) -> AgentCallContext:
        authorization = request.headers.getlist("authorization")
        session_references = request.headers.getlist(_SESSION_HEADER)
        if len(authorization) != 1 or len(session_references) != 1:
            raise HTTPException(status_code=401, detail="Service authentication is required.")
        scheme, separator, token = authorization[0].partition(" ")
        session_reference = session_references[0]
        if (
            scheme.lower() != "bearer"
            or not separator
            or not token
            or not _SESSION_REFERENCE.fullmatch(session_reference)
        ):
            raise HTTPException(status_code=401, detail="Service authentication is required.")
        try:
            service = service_token_validator.validate(token)
        except PortalAuthenticationError as exc:
            raise HTTPException(status_code=401, detail="Service authentication is invalid.") from exc

        now = _as_utc(clock())
        session = session_store.get_session(session_reference, now)
        if session is None:
            raise HTTPException(status_code=401, detail="Portal session is missing or expired.")
        if session.tenant_id != service.tenant_id:
            raise HTTPException(status_code=403, detail="Service and user tenants do not match.")
        return AgentCallContext(service=service, session=session)

    return resolve


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
