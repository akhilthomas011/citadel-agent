# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Local persistence of prepared change sets (``.citadel-agent/changes/<id>/``).

A prepared change set is written once and never modified: ``changeset.json`` plus a byte-exact
snapshot of every rendered file. Execution progress lives in a separate ``status.json``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citadel_agent.changes.changeset import ChangeSet

_SNAPSHOT_DIR = "files"


class ChangeStoreError(RuntimeError):
    """Raised when a change set is missing or would be overwritten."""


class ChangeStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _dir(self, change_id: str) -> Path:
        if not change_id.startswith("cs-") or "/" in change_id or "\\" in change_id or ".." in change_id:
            raise ChangeStoreError(f"Invalid change id '{change_id}'")
        return self.directory / change_id

    def save(self, change: ChangeSet, files: dict[str, str]) -> Path:
        """Persist a prepared change set and its rendered files. Refuses to overwrite."""
        target = self._dir(change.id)
        if target.exists():
            raise ChangeStoreError(f"Change set {change.id} already exists and is immutable")
        target.mkdir(parents=True)
        snapshot = target / _SNAPSHOT_DIR
        for relative, content in files.items():
            path = snapshot / Path(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        (target / "changeset.json").write_text(
            json.dumps(change.model_dump(mode="json", by_alias=True), indent=2), encoding="utf-8"
        )
        return target

    def load(self, change_id: str) -> ChangeSet:
        path = self._dir(change_id) / "changeset.json"
        if not path.is_file():
            raise ChangeStoreError(f"Change set {change_id} was not found")
        return ChangeSet.model_validate_json(path.read_text(encoding="utf-8"))

    def snapshot(self, change_id: str) -> dict[str, str]:
        """Return the rendered files captured at prepare time (repository-relative path to content)."""
        root = self._dir(change_id) / _SNAPSHOT_DIR
        return {
            path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def status(self, change_id: str) -> dict[str, Any]:
        path = self._dir(change_id) / "status.json"
        if not path.is_file():
            return {"steps": {}, "events": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def record(self, change_id: str, event: str, **details: Any) -> dict[str, Any]:
        """Append an execution event and merge step results into ``status.json``."""
        status = self.status(change_id)
        status["events"].append({"ts": datetime.now(UTC).isoformat(), "event": event, **details})
        step = details.get("step")
        if step:
            status["steps"].setdefault(step, {}).update({k: v for k, v in details.items() if k != "step"})
        for key in ("pullRequest", "recordedCommit", "state", "next", "recordError"):
            if key in details:
                status[key] = details[key]
        (self._dir(change_id) / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        return status

    def list_ids(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(
            (p.name for p in self.directory.iterdir() if p.is_dir() and p.name.startswith("cs-")),
            reverse=True,
        )
