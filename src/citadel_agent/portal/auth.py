# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Validated Microsoft Entra access tokens for the hosted portal API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from uuid import UUID

import jwt
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

from citadel_agent.portal.contracts import PortalRole

_SIGNING_ALGORITHM = "RS256"
_JWKS_TIMEOUT_SECONDS = 5


class PortalAuthenticationError(PermissionError):
    """The supplied access token cannot authenticate a portal user."""


@dataclass(frozen=True)
class EntraTokenSettings:
    """Trusted deployment configuration for one tenant and one portal API."""

    tenant_id: str
    audience: str
    allowed_client_ids: frozenset[str]
    required_scope: str = "access_as_user"

    def __post_init__(self) -> None:
        try:
            tenant_id = str(UUID(self.tenant_id))
        except ValueError as exc:
            raise ValueError("tenant_id must be a valid Entra tenant UUID") from exc
        if not self.audience.strip():
            raise ValueError("audience must not be empty")
        if not self.allowed_client_ids or any(not value.strip() for value in self.allowed_client_ids):
            raise ValueError("at least one allowed client ID must be configured")
        if not self.required_scope.strip():
            raise ValueError("required_scope must not be empty")
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(
            self,
            "allowed_client_ids",
            frozenset(value.lower() for value in self.allowed_client_ids),
        )

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    @property
    def jwks_uri(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"


@dataclass(frozen=True)
class PortalIdentity:
    """End-user identity derived only from a validated delegated access token."""

    subject_id: str
    tenant_id: str
    client_id: str
    expires_at: datetime
    scopes: frozenset[str]
    roles: frozenset[PortalRole]


@dataclass(frozen=True)
class PortalServiceIdentity:
    """An app-only service principal authorized to call the private agent API."""

    principal_id: str
    tenant_id: str
    client_id: str
    expires_at: datetime
    roles: frozenset[str]


class _TenantJwtVerifier:
    def __init__(
        self,
        settings: EntraTokenSettings,
        *,
        signing_key_resolver: Callable[[str], str | bytes] | None = None,
    ) -> None:
        self.settings = settings
        self._jwks = (
            PyJWKClient(settings.jwks_uri, timeout=_JWKS_TIMEOUT_SECONDS)
            if signing_key_resolver is None
            else None
        )
        self._signing_key_resolver = signing_key_resolver

    def decode(self, token: str, required_claims: list[str]) -> dict[str, object]:
        try:
            signing_key = (
                self._signing_key_resolver(token)
                if self._signing_key_resolver is not None
                else self._jwks.get_signing_key_from_jwt(token).key
            )
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=[_SIGNING_ALGORITHM],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": required_claims},
            )
        except (InvalidTokenError, PyJWKClientError) as exc:
            raise PortalAuthenticationError("The bearer token is invalid or expired.") from exc
        return claims


class EntraTokenValidator:
    """Validates a tenant-specific v2 delegated token using pinned Entra metadata."""

    def __init__(
        self,
        settings: EntraTokenSettings,
        *,
        signing_key_resolver: Callable[[str], str | bytes] | None = None,
    ) -> None:
        self.settings = settings
        self._verifier = _TenantJwtVerifier(settings, signing_key_resolver=signing_key_resolver)

    def validate(self, token: str) -> PortalIdentity:
        """Validate signature, algorithm, issuer, audience, lifetime and delegated claims."""
        claims = self._verifier.decode(
            token,
            ["aud", "exp", "iat", "iss", "oid", "scp", "tid", "azp"],
        )
        tenant_id = _claim_uuid(claims.get("tid"), "tenant")
        if tenant_id != self.settings.tenant_id:
            raise PortalAuthenticationError("The bearer token belongs to a different tenant.")

        subject_id = _claim_uuid(claims.get("oid"), "user")
        client_id = claims.get("azp")
        if not isinstance(client_id, str) or client_id.lower() not in self.settings.allowed_client_ids:
            raise PortalAuthenticationError("The bearer token was issued to an untrusted client.")

        raw_scopes = claims.get("scp")
        if not isinstance(raw_scopes, str):
            raise PortalAuthenticationError("Application-only tokens are not accepted.")
        scopes = frozenset(raw_scopes.split())
        if self.settings.required_scope not in scopes:
            raise PortalAuthenticationError("The bearer token lacks the required delegated scope.")

        raw_roles = claims.get("roles", [])
        if not isinstance(raw_roles, list) or any(not isinstance(role, str) for role in raw_roles):
            raise PortalAuthenticationError("The bearer token contains malformed application roles.")
        expiry = _expiration(claims.get("exp"))
        roles = frozenset(role for value in raw_roles if (role := _known_role(value)) is not None)
        return PortalIdentity(
            subject_id=subject_id,
            tenant_id=tenant_id,
            client_id=client_id.lower(),
            expires_at=expiry,
            scopes=scopes,
            roles=roles,
        )


class EntraServiceTokenValidator:
    """Validates app-only tokens for the BFF-to-agent service boundary."""

    def __init__(
        self,
        settings: EntraTokenSettings,
        *,
        required_app_role: str = "Portal.InvokeAgent",
        signing_key_resolver: Callable[[str], str | bytes] | None = None,
    ) -> None:
        if not required_app_role.strip():
            raise ValueError("required_app_role must not be empty")
        self.settings = settings
        self.required_app_role = required_app_role
        self._verifier = _TenantJwtVerifier(settings, signing_key_resolver=signing_key_resolver)

    def validate(self, token: str) -> PortalServiceIdentity:
        claims = self._verifier.decode(
            token,
            ["aud", "exp", "iat", "iss", "oid", "roles", "tid", "azp"],
        )
        tenant_id = _claim_uuid(claims.get("tid"), "tenant")
        if tenant_id != self.settings.tenant_id:
            raise PortalAuthenticationError("The bearer token belongs to a different tenant.")
        principal_id = _claim_uuid(claims.get("oid"), "service principal")
        client_id = claims.get("azp")
        if not isinstance(client_id, str) or client_id.lower() not in self.settings.allowed_client_ids:
            raise PortalAuthenticationError("The bearer token was issued to an untrusted service.")
        if "scp" in claims:
            raise PortalAuthenticationError("Delegated tokens are not accepted for service calls.")
        raw_roles = claims.get("roles")
        if not isinstance(raw_roles, list) or any(not isinstance(role, str) for role in raw_roles):
            raise PortalAuthenticationError("The service token contains malformed app roles.")
        roles = frozenset(raw_roles)
        if self.required_app_role not in roles:
            raise PortalAuthenticationError("The service token lacks the required app role.")
        return PortalServiceIdentity(
            principal_id=principal_id,
            tenant_id=tenant_id,
            client_id=client_id.lower(),
            expires_at=_expiration(claims.get("exp")),
            roles=roles,
        )


def _claim_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PortalAuthenticationError(f"The bearer token has no valid {label} identifier.")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise PortalAuthenticationError(f"The bearer token has no valid {label} identifier.") from exc


def _expiration(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise PortalAuthenticationError("The bearer token has no valid expiration.")
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise PortalAuthenticationError("The bearer token has no valid expiration.") from exc


def _known_role(value: str) -> PortalRole | None:
    try:
        return PortalRole(value)
    except ValueError:
        return None
