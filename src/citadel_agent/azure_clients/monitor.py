# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Log Analytics and Application Insights queries (KQL)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from azure.core.credentials import TokenCredential

DEFAULT_TIMESPAN = timedelta(days=1)


@dataclass(frozen=True)
class QueryResult:
    """Rows returned by a KQL query, as dictionaries keyed by column name."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    partial: bool = False
    error: str | None = None


def _tables_to_rows(tables: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in tables or []:
        columns = list(getattr(table, "columns", []))
        for row in getattr(table, "rows", []):
            rows.append(dict(zip(columns, list(row), strict=False)))
    return rows


class LogQuery:
    """Runs KQL against a Log Analytics workspace or a workspace-based Application Insights resource."""

    def __init__(self, credential: TokenCredential, *, client: Any = None) -> None:
        if client is None:
            from azure.monitor.query import LogsQueryClient

            client = LogsQueryClient(credential)
        self._client = client

    @staticmethod
    def _convert(response: Any) -> QueryResult:
        from azure.monitor.query import LogsQueryStatus

        status = getattr(response, "status", None)
        if status == LogsQueryStatus.PARTIAL:
            error = getattr(response, "partial_error", None)
            return QueryResult(
                rows=_tables_to_rows(getattr(response, "partial_data", [])),
                partial=True,
                error=getattr(error, "message", None) or (str(error) if error else None),
            )
        return QueryResult(rows=_tables_to_rows(getattr(response, "tables", [])))

    def query_workspace(
        self, workspace_id: str, kql: str, timespan: timedelta = DEFAULT_TIMESPAN
    ) -> QueryResult:
        """Query a Log Analytics workspace by its workspace (customer) ID."""
        return self._convert(self._client.query_workspace(workspace_id, kql, timespan=timespan))

    def query_resource(
        self, resource_id: str, kql: str, timespan: timedelta = DEFAULT_TIMESPAN
    ) -> QueryResult:
        """Query by Azure resource ID (for example an Application Insights component)."""
        return self._convert(self._client.query_resource(resource_id, kql, timespan=timespan))
