# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Server-side Entra sign-in and opaque browser sessions for the portal BFF."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from msal import ConfidentialClientApplication
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse

from citadel_agent.portal.auth import (
    EntraTokenValidator,
    PortalAuthenticationError,
    PortalIdentity,
)
from citadel_agent.portal.sessions import AuthFlowRecord, PortalSessionRecord, PortalSessionStore

SESSION_COOKIE = "__Host-citadel_session"
AUTH_TRANSACTION_COOKIE = "__Host-citadel_auth_tx"
CSRF_HEADER = "X-CSRF-Token"
_COOKIE_SECRET = re.compile(r"^[A-Za-z0-9_-]{43}$")


class PortalOAuthError(PermissionError):
    """Entra rejected or could not complete a portal sign-in."""


@dataclass(frozen=True)
class PortalAuthConfig:
    """Canonical public origin and bounded lifetimes for BFF authentication."""

    origin: str
    callback_uri: str
    session_lifetime_seconds: int = 900
    auth_flow_lifetime_seconds: int = 300

    def __post_init__(self) -> None:
        origin = urlsplit(self.origin)
        callback = urlsplit(self.callback_uri)
        if (
            origin.scheme != "https"
            or not origin.netloc
            or origin.path
            or origin.query
            or origin.fragment
            or origin.username
            or origin.password
        ):
            raise ValueError("portal origin must be a canonical HTTPS origin")
        if (
            callback.scheme != "https"
            or callback.netloc.lower() != origin.netloc.lower()
            or callback.path != "/auth/callback"
            or callback.query
            or callback.fragment
            or callback.username
            or callback.password
        ):
            raise ValueError("callback_uri must be the same-origin /auth/callback HTTPS URL")
        if not 60 <= self.session_lifetime_seconds <= 3600:
            raise ValueError("session lifetime must be between 60 and 3600 seconds")
        if not 60 <= self.auth_flow_lifetime_seconds <= 600:
            raise ValueError("auth flow lifetime must be between 60 and 600 seconds")


class MsalOAuthClient:
    """Small adapter that never persists MSAL tokens or its token cache."""

    def __init__(
        self,
        application: ConfidentialClientApplication,
        *,
        scopes: tuple[str, ...],
        redirect_uri: str,
    ) -> None:
        self._application = application
        self._scopes = list(scopes)
        self._redirect_uri = redirect_uri

    def begin(self) -> Mapping[str, object]:
        flow = self._application.initiate_auth_code_flow(
            scopes=self._scopes,
            redirect_uri=self._redirect_uri,
            response_mode="form_post",
        )
        if not isinstance(flow, dict) or "error" in flow:
            raise PortalOAuthError("Entra sign-in could not be started.")
        return flow

    def complete(self, flow: Mapping[str, object], response: Mapping[str, str]) -> str:
        try:
            result = self._application.acquire_token_by_auth_code_flow(
                dict(flow),
                dict(response),
                scopes=self._scopes,
            )
        except ValueError as exc:
            raise PortalOAuthError("Entra sign-in response was invalid.") from exc
        if not isinstance(result, dict) or "error" in result:
            raise PortalOAuthError("Entra sign-in was not completed.")
        access_token = result.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise PortalOAuthError("Entra sign-in did not return an API access token.")
        return access_token


class OAuthClient(Protocol):
    def begin(self) -> Mapping[str, object]: ...

    def complete(self, flow: Mapping[str, object], response: Mapping[str, str]) -> str: ...


def create_auth_router(
    *,
    config: PortalAuthConfig,
    oauth_client: OAuthClient,
    token_validator: EntraTokenValidator,
    store: PortalSessionStore,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    """Build the BFF auth routes around a durable store and trusted Entra configuration."""
    router = APIRouter(prefix="/auth", tags=["authentication"])

    @router.get("/login", include_in_schema=False)
    def login() -> Response:
        try:
            flow = oauth_client.begin()
        except PortalOAuthError as exc:
            raise HTTPException(status_code=503, detail="Sign-in is temporarily unavailable.") from exc
        auth_uri = flow.get("auth_uri")
        if not isinstance(auth_uri, str) or not _is_tenant_authority(auth_uri, token_validator):
            raise HTTPException(status_code=503, detail="Sign-in authority is misconfigured.")

        now = _as_utc(clock())
        transaction_secret = secrets.token_urlsafe(32)
        record = AuthFlowRecord(
            id=_digest(transaction_secret),
            flow=dict(flow),
            expires_at=now + timedelta(seconds=config.auth_flow_lifetime_seconds),
            ttl=config.auth_flow_lifetime_seconds,
        )
        store.create_auth_flow(record)

        response = RedirectResponse(auth_uri, status_code=303)
        response.set_cookie(
            AUTH_TRANSACTION_COOKIE,
            transaction_secret,
            max_age=config.auth_flow_lifetime_seconds,
            httponly=True,
            secure=True,
            samesite="none",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/callback", include_in_schema=False)
    async def callback(request: Request) -> Response:
        transaction_secret = request.cookies.get(AUTH_TRANSACTION_COOKIE)
        if not _is_cookie_secret(transaction_secret):
            raise HTTPException(status_code=400, detail="Sign-in transaction is missing or expired.")
        auth_response = await _form_parameters(request)
        now = _as_utc(clock())
        flow_record = await run_in_threadpool(store.consume_auth_flow, _digest(transaction_secret), now)
        if flow_record is None:
            raise HTTPException(status_code=400, detail="Sign-in transaction is missing or expired.")

        try:
            access_token = await run_in_threadpool(oauth_client.complete, flow_record.flow, auth_response)
            identity = await run_in_threadpool(token_validator.validate, access_token)
        except (PortalOAuthError, PortalAuthenticationError) as exc:
            raise HTTPException(status_code=401, detail="Sign-in could not be verified.") from exc

        expires_at = min(
            now + timedelta(seconds=config.session_lifetime_seconds),
            identity.expires_at,
        )
        ttl = int((expires_at - now).total_seconds())
        if ttl < 1:
            raise HTTPException(status_code=401, detail="Sign-in has expired.")
        session_secret = secrets.token_urlsafe(32)
        session = _session_record(_digest(session_secret), identity, now, expires_at, ttl)
        await run_in_threadpool(store.create_session, session)

        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session_secret,
            max_age=ttl,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        response.delete_cookie(
            AUTH_TRANSACTION_COOKIE,
            httponly=True,
            secure=True,
            samesite="none",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/session")
    def session_status(request: Request) -> Response:
        session = _request_session(request, store, clock)
        if session is None:
            payload: dict[str, object] = {"authenticated": False}
        else:
            payload = {
                "authenticated": True,
                "user": {
                    "tenantId": session.tenant_id,
                    "subjectId": session.subject_id,
                    "roles": sorted(role.value for role in session.roles),
                },
                "csrfToken": session.csrf_token,
                "expiresAt": session.expires_at.isoformat(),
            }
        response = JSONResponse(payload)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/logout", status_code=204, include_in_schema=False)
    def logout(request: Request) -> Response:
        session = _request_session(request, store, clock)
        _require_csrf(request, session, config)
        store.delete_session(session.id)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, httponly=True, secure=True, samesite="lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    return router


def _session_record(
    record_id: str,
    identity: PortalIdentity,
    now: datetime,
    expires_at: datetime,
    ttl: int,
) -> PortalSessionRecord:
    return PortalSessionRecord(
        id=record_id,
        tenant_id=identity.tenant_id,
        subject_id=identity.subject_id,
        client_id=identity.client_id,
        roles=identity.roles,
        scopes=identity.scopes,
        csrf_token=secrets.token_urlsafe(32),
        created_at=now,
        expires_at=expires_at,
        ttl=ttl,
    )


def _request_session(
    request: Request,
    store: PortalSessionStore,
    clock: Callable[[], datetime],
) -> PortalSessionRecord | None:
    resolved = resolve_browser_session(request, store, clock)
    return None if resolved is None else resolved[1]


def resolve_browser_session(
    request: Request,
    store: PortalSessionStore,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[str, PortalSessionRecord] | None:
    """Resolve a valid opaque browser cookie to its server-side reference and session."""
    secret = request.cookies.get(SESSION_COOKIE)
    if secret is None or not _COOKIE_SECRET.fullmatch(secret):
        return None
    session_reference = _digest(secret)
    session = store.get_session(session_reference, _as_utc(clock()))
    if session is None:
        return None
    return session_reference, session


def _require_csrf(
    request: Request,
    session: PortalSessionRecord | None,
    config: PortalAuthConfig,
) -> None:
    if session is None:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    csrf_token = request.headers.get(CSRF_HEADER, "")
    origin = request.headers.get("Origin", "")
    if origin != config.origin or not hmac.compare_digest(csrf_token, session.csrf_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")


def _is_cookie_secret(value: str | None) -> bool:
    return value is not None and _COOKIE_SECRET.fullmatch(value) is not None


def _is_tenant_authority(uri: str, validator: EntraTokenValidator) -> bool:
    parsed = urlsplit(uri)
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() == "login.microsoftonline.com"
        and parsed.path == f"/{validator.settings.tenant_id}/oauth2/v2.0/authorize"
        and not parsed.username
        and not parsed.password
    )


async def _form_parameters(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
        "application/x-www-form-urlencoded"
    ):
        raise HTTPException(status_code=400, detail="Invalid sign-in response.")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > 16_384:
                raise HTTPException(status_code=413, detail="Sign-in response is too large.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid sign-in response.") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16_384:
            raise HTTPException(status_code=413, detail="Sign-in response is too large.")
    try:
        parsed = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=32,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid sign-in response.") from exc
    if not parsed or any(len(values) != 1 for values in parsed.values()):
        raise HTTPException(status_code=400, detail="Invalid sign-in response.")
    return {key: values[0] for key, values in parsed.items()}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
