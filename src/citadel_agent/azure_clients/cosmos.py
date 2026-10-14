# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Read access to the Citadel usage store in Azure Cosmos DB (Microsoft Entra ID auth)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from azure.core.credentials import TokenCredential

LLM_USAGE_CONTAINER = "llm-usage-container"
MODEL_PRICING_CONTAINER = "model-pricing"
MCP_USAGE_CONTAINER = "mcp-usage-container"
AGENT_USAGE_CONTAINER = "agent-usage-container"


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

    def list_containers(self, database: str) -> list[str]:
        db = self._client.get_database_client(database)
        return sorted(c["id"] for c in db.list_containers())

    def discover_usage_database(self) -> str | None:
        """Return the database that holds ``llm-usage-container`` (upstream docs disagree on its name)."""
        for database in self.list_databases():
            if LLM_USAGE_CONTAINER in self.list_containers(database):
                return database
        return None

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

    def upsert(self, database: str, container: str, item: Mapping[str, Any]) -> None:
        """Create or replace one document. Only the pricing command uses this; every other caller reads."""
        client = self._client.get_database_client(database).get_container_client(container)
        client.upsert_item(dict(item))
