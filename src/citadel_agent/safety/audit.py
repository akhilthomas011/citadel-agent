# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Append-only JSONL audit log of agent actions.

Every tool invocation, approval decision, deployment and pull request is recorded with redacted
details under ``.citadel-agent/audit/audit-<date>.jsonl``.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from citadel_agent.safety.redaction import Redactor, default_redactor


class AuditOutcome(StrEnum):
    """Outcome of an audited event."""

    OK = "ok"
    ERROR = "error"
    DENIED = "denied"
    APPROVED = "approved"
    REJECTED = "rejected"


class AuditLog:
    """Thread-safe JSONL audit log writer."""

    def __init__(
        self,
        directory: Path,
        *,
        session_id: str | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.directory = directory
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self._redactor = redactor or default_redactor()
        self._lock = threading.Lock()

    def _path_for(self, timestamp: datetime) -> Path:
        return self.directory / f"audit-{timestamp:%Y-%m-%d}.jsonl"

    def record(
        self,
        event: str,
        *,
        outcome: AuditOutcome = AuditOutcome.OK,
        tool: str | None = None,
        actor: str | None = None,
        details: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Append one audit entry and return it (already redacted).

        Args:
            event: Short event name, for example ``tool.call`` or ``change.execute``.
            outcome: Result of the event.
            tool: Tool name, when the event relates to a tool.
            actor: Signed-in principal performing the action.
            details: Additional context; redacted before writing.
            now: Timestamp override for tests.
        """
        timestamp = now or datetime.now(UTC)
        entry: dict[str, Any] = {
            "ts": timestamp.isoformat(),
            "session": self.session_id,
            "event": event,
            "outcome": str(outcome),
        }
        if tool:
            entry["tool"] = tool
        if actor:
            entry["actor"] = actor
        if details:
            entry["details"] = self._redactor.redact_obj(dict(details))

        line = json.dumps(entry, default=str, ensure_ascii=False)
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._path_for(timestamp).open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return entry
