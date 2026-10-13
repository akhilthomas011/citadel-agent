# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Azure credential creation and caller identification.

Locally the agent acts as the signed-in Azure CLI user, so it can only do what that user's
Azure RBAC allows. Hosted deployments can switch to managed identity through
``CITADEL_AGENT_CREDENTIAL``.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any

from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential, DefaultAzureCredential, ManagedIdentityCredential

ENV_CREDENTIAL = "CITADEL_AGENT_CREDENTIAL"
ENV_MANAGED_IDENTITY_CLIENT_ID = "CITADEL_AGENT_MANAGED_IDENTITY_CLIENT_ID"
ARM_SCOPE = "https://management.azure.com/.default"


class CredentialConfigurationError(ValueError):
    """Raised when the credential kind is not recognized."""


def create_credential(kind: str | None = None) -> TokenCredential:
    """Create the Azure credential the agent uses.

    Args:
        kind: ``cli`` (default, the signed-in Azure CLI user), ``managed-identity`` or ``default``
            (``DefaultAzureCredential`` without interactive browser sign-in). Read from
            ``CITADEL_AGENT_CREDENTIAL`` when omitted.
    """
    selected = (kind or os.environ.get(ENV_CREDENTIAL) or "cli").strip().lower()
    if selected == "cli":
        return AzureCliCredential(process_timeout=60)
    if selected == "managed-identity":
        client_id = os.environ.get(ENV_MANAGED_IDENTITY_CLIENT_ID) or None
        return ManagedIdentityCredential(client_id=client_id)
    if selected == "default":
        return DefaultAzureCredential(exclude_interactive_browser_credential=True)
    raise CredentialConfigurationError(
        f"Unknown credential kind '{selected}'. Use cli, managed-identity or default."
    )


@dataclass(frozen=True)
class PrincipalInfo:
    """Identity of the caller, decoded from an access token (claims only, no secrets)."""

    object_id: str | None
    tenant_id: str | None
    display_name: str | None
    principal_type: str

    @property
    def label(self) -> str:
        return self.display_name or self.object_id or "unknown"


def decode_token_claims(token: str) -> dict[str, Any]:
    """Decode the claims of a JWT without validating it (for caller identification only)."""
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (IndexError, ValueError) as exc:
        raise ValueError("Access token is not a decodable JWT.") from exc


def describe_principal(credential: TokenCredential, scope: str = ARM_SCOPE) -> PrincipalInfo:
    """Return the identity behind ``credential`` by decoding an access token's claims."""
    claims = decode_token_claims(credential.get_token(scope).token)
    is_user = "upn" in claims or "unique_name" in claims or claims.get("idtyp") == "user"
    return PrincipalInfo(
        object_id=claims.get("oid"),
        tenant_id=claims.get("tid"),
        display_name=claims.get("upn") or claims.get("unique_name") or claims.get("appid"),
        principal_type="User" if is_user else "ServicePrincipal",
    )
