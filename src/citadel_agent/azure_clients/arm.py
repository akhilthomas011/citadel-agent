# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Minimal Azure Resource Manager REST client.

Used for read-only calls that do not justify a dedicated SDK: effective permissions at a scope,
resource discovery and Microsoft Foundry (Cognitive Services) deployment listing.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from azure.core.credentials import TokenCredential

from citadel_agent.azure_clients.credentials import ARM_SCOPE

ARM_ENDPOINT = "https://management.azure.com"
PERMISSIONS_API_VERSION = "2022-04-01"
RESOURCES_API_VERSION = "2021-04-01"
COGNITIVE_API_VERSION = "2024-10-01"
DEFAULT_TIMEOUT = httpx.Timeout(60.0)


class ArmError(RuntimeError):
    """Raised when an ARM request fails."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class PermissionEntry:
    """Permissions granted by one role assignment (as returned by the ARM permissions API)."""

    actions: tuple[str, ...] = ()
    not_actions: tuple[str, ...] = ()
    data_actions: tuple[str, ...] = ()
    not_data_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PermissionSet:
    """Effective RBAC permissions of the caller at a scope.

    Azure evaluates ``actions`` minus ``notActions`` per role assignment and grants the union of
    the results, so entries are evaluated independently.
    """

    entries: tuple[PermissionEntry, ...]

    def allows(self, action: str) -> bool:
        """Return True when any role assignment grants the control-plane ``action``."""
        return any(_granted(action, e.actions, e.not_actions) for e in self.entries)

    def allows_data(self, action: str) -> bool:
        """Return True when any role assignment grants the data-plane ``action``."""
        return any(_granted(action, e.data_actions, e.not_data_actions) for e in self.entries)


def _matches(action: str, patterns: Iterable[str]) -> bool:
    lowered = action.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in patterns)


def _granted(action: str, allowed: Iterable[str], denied: Iterable[str]) -> bool:
    return _matches(action, allowed) and not _matches(action, denied)


def merge_permissions(entries: Iterable[Mapping[str, Any]]) -> PermissionSet:
    """Build a :class:`PermissionSet` from the entries returned by the ARM permissions API."""
    return PermissionSet(
        tuple(
            PermissionEntry(
                actions=tuple(entry.get("actions", [])),
                not_actions=tuple(entry.get("notActions", [])),
                data_actions=tuple(entry.get("dataActions", [])),
                not_data_actions=tuple(entry.get("notDataActions", [])),
            )
            for entry in entries
        )
    )


class ArmClient:
    """Synchronous ARM REST client authenticated with an Azure credential."""

    def __init__(
        self,
        credential: TokenCredential,
        *,
        endpoint: str = ARM_ENDPOINT,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._credential = credential
        self._endpoint = endpoint.rstrip("/")
        self._http = http_client or httpx.Client(timeout=DEFAULT_TIMEOUT)

    def _headers(self) -> dict[str, str]:
        token = self._credential.get_token(ARM_SCOPE).token
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith("https://"):
            return path_or_url
        return f"{self._endpoint}/{path_or_url.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        api_version: str | None,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send a request and return the decoded JSON body (empty dict for empty bodies)."""
        query = dict(params or {})
        if api_version:
            query["api-version"] = api_version
        response = self._http.request(
            method, self._url(path), headers=self._headers(), params=query, json=json
        )
        if response.status_code >= 400:
            code = None
            message = response.text[:500]
            try:
                error = response.json().get("error", {})
                code = error.get("code")
                message = error.get("message", message)
            except ValueError:
                pass
            raise ArmError(
                f"ARM {method} {path} failed ({response.status_code}): {message}",
                status_code=response.status_code,
                code=code,
            )
        return response.json() if response.content else {}

    def get(self, path: str, api_version: str, params: Mapping[str, str] | None = None) -> dict[str, Any]:
        return self.request("GET", path, api_version, params=params)

    def list(
        self, path: str, api_version: str, params: Mapping[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """GET a collection and follow ``nextLink`` pages."""
        items: list[dict[str, Any]] = []
        page = self.get(path, api_version, params)
        items.extend(page.get("value", []))
        next_link = page.get("nextLink")
        while next_link:
            page = self.request("GET", next_link, None)
            items.extend(page.get("value", []))
            next_link = page.get("nextLink")
        return items

    def permissions(self, scope: str) -> PermissionSet:
        """Return the caller's effective permissions at an ARM scope (resource ID or subscription)."""
        entries = self.list(
            f"{scope.rstrip('/')}/providers/Microsoft.Authorization/permissions", PERMISSIONS_API_VERSION
        )
        return merge_permissions(entries)

    def list_resources(
        self,
        subscription_id: str,
        resource_group: str,
        resource_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """List resources in a resource group, optionally filtered by type (``Microsoft.KeyVault/vaults``)."""
        params = {"$filter": f"resourceType eq '{resource_type}'"} if resource_type else None
        return self.list(
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/resources",
            RESOURCES_API_VERSION,
            params,
        )

    def list_cognitive_accounts(self, subscription_id: str) -> list[dict[str, Any]]:
        """List Microsoft Foundry / Azure OpenAI (Cognitive Services) accounts in a subscription."""
        return self.list(
            f"/subscriptions/{subscription_id}/providers/Microsoft.CognitiveServices/accounts",
            COGNITIVE_API_VERSION,
        )

    def list_cognitive_deployments(self, account_id: str) -> list[dict[str, Any]]:
        """List model deployments of a Cognitive Services account (by full resource ID)."""
        return self.list(f"{account_id.rstrip('/')}/deployments", COGNITIVE_API_VERSION)

    def close(self) -> None:
        self._http.close()
