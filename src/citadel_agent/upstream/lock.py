# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Read, build and verify ``citadel.lock.json``.

The lock file records exactly which upstream Citadel commit the agent was built and tested
against, together with a snapshot of that commit's ``release.json`` version tracks. Every change
set the agent produces carries this information so upgrades are explicit and reviewable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from citadel_agent.paths import DEFAULT_UPSTREAM_SUBMODULE, lock_file_path
from citadel_agent.proc import run_command

RELEASE_FILE_NAME = "release.json"
LOCK_SCHEMA_VERSION = 1


class UpstreamPin(BaseModel):
    """The upstream repository commit the agent is pinned to."""

    model_config = ConfigDict(extra="forbid")

    repository: str
    branch: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    path: str
    release: dict[str, str]


class UpstreamLock(BaseModel):
    """Contents of ``citadel.lock.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = LOCK_SCHEMA_VERSION
    upstream: UpstreamPin
    synced_at: datetime


@dataclass(frozen=True)
class LockStatus:
    """Comparison between the lock file and the checked-out upstream submodule."""

    locked_commit: str
    checked_out_commit: str
    locked_release: dict[str, str]
    checked_out_release: dict[str, str]

    @property
    def commit_matches(self) -> bool:
        return self.locked_commit == self.checked_out_commit

    @property
    def release_matches(self) -> bool:
        return self.locked_release == self.checked_out_release

    @property
    def in_sync(self) -> bool:
        return self.commit_matches and self.release_matches


def read_release(upstream_dir: Path) -> dict[str, str]:
    """Read the upstream ``release.json`` version tracks.

    Args:
        upstream_dir: Root of the upstream checkout.

    Returns:
        Mapping of track name (for example ``access-contract-version``) to version string.
    """
    data = json.loads((upstream_dir / RELEASE_FILE_NAME).read_text(encoding="utf-8"))
    return {str(key): str(value) for key, value in data.items()}


def read_lock(root: Path) -> UpstreamLock:
    """Load and validate ``citadel.lock.json`` from the checkout root."""
    return UpstreamLock.model_validate_json(lock_file_path(root).read_text(encoding="utf-8"))


def write_lock(root: Path, lock: UpstreamLock) -> Path:
    """Write ``citadel.lock.json`` to the checkout root and return its path."""
    path = lock_file_path(root)
    payload = lock.model_dump(mode="json")
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _git(cwd: Path, *args: str) -> str:
    return run_command("git", args, cwd=cwd).stdout.strip()


def submodule_commit(root: Path, submodule_path: str = DEFAULT_UPSTREAM_SUBMODULE) -> str:
    """Return the commit currently checked out in the upstream submodule."""
    return _git(root / submodule_path, "rev-parse", "HEAD")


def submodule_settings(root: Path, submodule_path: str = DEFAULT_UPSTREAM_SUBMODULE) -> tuple[str, str]:
    """Return the ``(url, branch)`` configured for the submodule in ``.gitmodules``."""
    key = f"submodule.{submodule_path}"
    url = _git(root, "config", "-f", ".gitmodules", "--get", f"{key}.url")
    branch = _git(root, "config", "-f", ".gitmodules", "--get", f"{key}.branch")
    return url, branch


def build_lock(
    root: Path,
    submodule_path: str = DEFAULT_UPSTREAM_SUBMODULE,
    *,
    now: datetime | None = None,
) -> UpstreamLock:
    """Build a lock describing the currently checked-out upstream submodule.

    Args:
        root: Agent checkout root.
        submodule_path: Path of the upstream submodule relative to ``root``.
        now: Timestamp to record; defaults to the current UTC time.

    Returns:
        A new :class:`UpstreamLock`.
    """
    url, branch = submodule_settings(root, submodule_path)
    return UpstreamLock(
        upstream=UpstreamPin(
            repository=url,
            branch=branch,
            commit=submodule_commit(root, submodule_path),
            path=submodule_path,
            release=read_release(root / submodule_path),
        ),
        synced_at=now or datetime.now(UTC),
    )


def check_lock(root: Path) -> LockStatus:
    """Compare the lock file with the checked-out upstream submodule."""
    lock = read_lock(root)
    return LockStatus(
        locked_commit=lock.upstream.commit,
        checked_out_commit=submodule_commit(root, lock.upstream.path),
        locked_release=lock.upstream.release,
        checked_out_release=read_release(root / lock.upstream.path),
    )
