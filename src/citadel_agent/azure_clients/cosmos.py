# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Read access to the Citadel usage store in Azure Cosmos DB (Microsoft Entra ID auth)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from azure.core.credentials import TokenCredential

from citadel_agent.analytics.cursors import (
    ScopedCosmosCursorSigner,
    cosmos_query_fingerprint,
)
from citadel_agent.portal.contracts import HubScope

LLM_USAGE_CONTAINER = "llm-usage-container"
MODEL_PRICING_CONTAINER = "model-pricing"
MCP_USAGE_CONTAINER = "mcp-usage-container"
AGENT_USAGE_CONTAINER = "agent-usage-container"


@dataclass(frozen=True)
class CosmosQueryPage:
    """One Cosmos query page and its opaque continuation token, if more data remains."""

    items: tuple[dict[str, Any], ...]
    continuation_token: str | None


@dataclass(frozen=True)
class ScopedCosmosQueryPage:
    """One bounded Cosmos page and a continuation cursor bound to its query and hub scope."""

    items: tuple[dict[str, Any], ...]
    cursor: str | None


def cosmos_endpoint(account_name: str) -> str:
    """Return the data-plane endpoint of a Cosmos DB account in the Azure public cloud."""
    return f"https://{account_name}.documents.azure.com:443/"


class CosmosUsageReader:
    """Queries usage and pricing containers. The database name is discovered at runtime."""

    def __init__(self, credential: TokenCredential, endpoint: str, *, client: Any = None) -> None:
        if client is None:
            from azure.cosmos import CosmosClient

            client = CosmosClient(endpoint, credential=credential)
        self._client = client
        self.endpoint = endpoint

    def list_databases(self) -> list[str]:
        return sorted(db["id"] for db in self._client.list_databases())

    def close(self) -> None:
        self._client.close()

    def list_containers(self, database: str) -> list[str]:
        db = self._client.get_database_client(database)
        return sorted(c["id"] for c in db.list_containers())

    def discover_usage_database(self) -> str | None:
        """Return the database that holds ``llm-usage-container`` (upstream docs disagree on its name)."""
        for database in self.list_databases():
            if LLM_USAGE_CONTAINER in self.list_containers(database):
                return database
        return None

    def read_feed_ranges(self, database: str, container: str) -> tuple[dict[str, Any], ...]:
        """Return opaque SDK routing ranges for restartable cross-partition page processing."""
        client = self._client.get_database_client(database).get_container_client(container)
        ranges = tuple(dict(value) for value in client.read_feed_ranges())
        if not ranges:
            raise RuntimeError("Cosmos did not return routing ranges; source completeness is unknown.")
        return ranges

    def query(
        self,
        database: str,
        container: str,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        max_items: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Run a parameterized SQL query across partitions (parameters are passed, never interpolated)."""
        if max_items < 1:
            raise ValueError("max_items must be greater than zero")
        client = self._client.get_database_client(database).get_container_client(container)
        params = [
            {"name": name if name.startswith("@") else f"@{name}", "value": value}
            for name, value in (parameters or {}).items()
        ]
        results: list[dict[str, Any]] = []
        for item in client.query_items(query=sql, parameters=params, enable_cross_partition_query=True):
            results.append(item)
            if len(results) >= max_items:
                break
        return results

    def query_page(
        self,
        database: str,
        container: str,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        page_size: int = 500,
        continuation_token: str | None = None,
        feed_range: dict[str, Any] | None = None,
    ) -> CosmosQueryPage:
        """Read one bounded page; callers must follow the token to process the complete result set."""
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        client = self._client.get_database_client(database).get_container_client(container)
        params = [
            {"name": name if name.startswith("@") else f"@{name}", "value": value}
            for name, value in (parameters or {}).items()
        ]
        results = client.query_items(
            query=sql,
            parameters=params,
            enable_cross_partition_query=True,
            max_item_count=page_size,
            **({"feed_range": feed_range} if feed_range is not None else {}),
        )
        pages = results.by_page(continuation_token=continuation_token)
        items = tuple(next(pages, ()))
        token = pages.continuation_token
        if token is not None and not isinstance(token, str):
            raise RuntimeError("Cosmos returned an unsupported continuation token.")
        return CosmosQueryPage(
            items=items,
            continuation_token=token if isinstance(token, str) and token else None,
        )

    def query_scoped_page(
        self,
        database: str,
        container: str,
        sql: str,
        parameters: Mapping[str, Any] | None,
        *,
        scope: HubScope,
        cursor_signer: ScopedCosmosCursorSigner,
        cursor: str | None = None,
        page_size: int = 500,
    ) -> ScopedCosmosQueryPage:
        """Read a page using a signed cursor bound to the caller's authorized scope and query."""
        fingerprint = cosmos_query_fingerprint(
            sql,
            parameters,
            account_endpoint=self.endpoint,
            database=database,
            container=container,
            page_size=page_size,
        )
        continuation_token = (
            cursor_signer.read(cursor, scope=scope, query_fingerprint=fingerprint)
            if cursor is not None
            else None
        )
        page = self.query_page(
            database,
            container,
            sql,
            parameters,
            page_size=page_size,
            continuation_token=continuation_token,
        )
        next_cursor = (
            cursor_signer.issue(
                page.continuation_token,
                scope=scope,
                query_fingerprint=fingerprint,
            )
            if page.continuation_token is not None
            else None
        )
        return ScopedCosmosQueryPage(items=page.items, cursor=next_cursor)

    def upsert(self, database: str, container: str, item: Mapping[str, Any]) -> None:
        """Create or replace one document. Only the pricing command uses this; every other caller reads."""
        client = self._client.get_database_client(database).get_container_client(container)
        client.upsert_item(dict(item))
