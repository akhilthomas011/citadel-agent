# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Key Vault access for contract credentials.

Tools hand users the *names* of Key Vault secrets, never their values. Values are read only for
in-memory use (smoke tests, the agent's own gateway key, key-rotation verification) and are
registered with the redactor as soon as they are read.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, TypeVar

from azure.core.credentials import TokenCredential
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from citadel_agent.safety.redaction import Redactor, default_redactor

T = TypeVar("T")
_CLIENT_ADDRESS = re.compile(r"Client address:\s*([0-9A-Fa-f:.]+)")


class KeyVaultAccessError(RuntimeError):
    """Raised when Key Vault refuses a request, with guidance on how to fix it."""


def explain_key_vault_error(exc: HttpResponseError, url: str) -> KeyVaultAccessError:
    """Translate a Key Vault denial (network rules or RBAC) into an actionable error."""
    text = str(exc)
    if "ForbiddenByFirewall" in text or "Client address is not authorized" in text:
        match = _CLIENT_ADDRESS.search(text)
        address = match.group(1) if match else "your client address"
        return KeyVaultAccessError(
            f"Key Vault {url} blocked the request from {address} (network rules). For local "
            "development, add that address to the vault's firewall (dev environments only), or run "
            "the agent from inside the hub network."
        )
    if "ForbiddenByRbac" in text or getattr(exc, "status_code", None) == 403:
        return KeyVaultAccessError(
            f"You are not allowed to read secrets in {url}. Ask for the 'Key Vault Secrets User' "
            "role on the vault."
        )
    return KeyVaultAccessError(f"Key Vault {url} request failed: {default_redactor().redact(text)[:300]}")


def vault_url(vault_name: str) -> str:
    """Return the data-plane URL of a Key Vault in the Azure public cloud."""
    return f"https://{vault_name}.vault.azure.net/"


class KeyVaultReader:
    """Reads secret metadata and, for in-memory use only, secret values."""

    def __init__(
        self,
        credential: TokenCredential,
        url: str,
        *,
        client: Any = None,
        redactor: Redactor | None = None,
    ) -> None:
        if client is None:
            from azure.keyvault.secrets import SecretClient

            client = SecretClient(vault_url=url, credential=credential)
        self._client = client
        self.url = url
        self._redactor = redactor or default_redactor()

    def _call(self, operation: Callable[[], T]) -> T:
        try:
            return operation()
        except ResourceNotFoundError:
            raise
        except HttpResponseError as exc:
            raise explain_key_vault_error(exc, self.url) from exc

    def list_secret_names(self) -> list[str]:
        """Return the names of enabled secrets."""
        return self._call(
            lambda: sorted(
                p.name for p in self._client.list_properties_of_secrets() if getattr(p, "enabled", True)
            )
        )

    def secret_exists(self, name: str) -> bool:
        try:
            self.get_secret_in_memory(name)
        except ResourceNotFoundError:
            return False
        return True

    def get_secret_in_memory(self, name: str) -> str:
        """Return a secret value for in-memory use. The value is registered for redaction."""
        value = self._call(lambda: self._client.get_secret(name).value) or ""
        self._redactor.register_secret(value)
        return value
