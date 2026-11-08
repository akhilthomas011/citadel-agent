# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Log Analytics and Application Insights queries (KQL)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError

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

    def close(self) -> None:
        self._client.close()

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
        self,
        workspace_id: str,
        kql: str,
        timespan: timedelta | tuple[datetime, datetime] = DEFAULT_TIMESPAN,
    ) -> QueryResult:
        """Query a Log Analytics workspace by its workspace (customer) ID."""
        return self._convert(self._client.query_workspace(workspace_id, kql, timespan=timespan))

    def query_resource(
        self,
        resource_id: str,
        kql: str,
        timespan: timedelta | tuple[datetime, datetime] = DEFAULT_TIMESPAN,
    ) -> QueryResult:
        """Query by Azure resource ID (for example an Application Insights component)."""
        return self._convert(self._client.query_resource(resource_id, kql, timespan=timespan))


class MonitorReadError(AzureError):
    def __init__(self, status_code: int | None = None):
        super().__init__("The Azure Monitor read is unavailable.")
        self.status_code = status_code


class AlertReader:
    """Only filtered alert reads; never alert-state changes or arbitrary next-link hosts."""

    def __init__(self, credential: TokenCredential, *, client: httpx.Client | None = None) -> None:
        self.credential = credential
        self.client = client or httpx.Client(timeout=30, trust_env=False)
        self.owned_client = client is None

    def close(self) -> None:
        if self.owned_client:
            self.client.close()

    def read(self, subscription: str, resource_id: str, start: datetime, end: datetime) -> QueryResult:
        path = f"/subscriptions/{subscription}/providers/Microsoft.AlertsManagement/alerts"
        token = self.credential.get_token("https://management.azure.com/.default")
        try:
            response = self.client.get(
                "https://management.azure.com" + path,
                headers={"Authorization": f"Bearer {token.token}"},
                params={
                    "api-version": "2019-03-01",
                    "targetResource": resource_id,
                    "monitorCondition": "Fired",
                    "includeContext": "false",
                    "pageCount": "100",
                    "customTimeRange": f"{start.isoformat()}/{end.isoformat()}",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MonitorReadError(exc.response.status_code) from exc
        except httpx.RequestError as exc:
            raise MonitorReadError() from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise MonitorReadError() from exc
        if not isinstance(body, dict) or not isinstance(body.get("value"), list):
            raise MonitorReadError()
        rows = body["value"]
        if any(not isinstance(row, dict) for row in rows):
            raise MonitorReadError()
        return QueryResult(rows=rows[:100], partial=bool(body.get("nextLink")) or len(rows) > 100)
