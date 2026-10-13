# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""The canonical contracts repository: the single source of truth for every agent change.

Layout::

    upstream/ai-hub-gateway/           pinned upstream Citadel (git submodule)
    contracts/access/<bu>/<uc>/<env>/  contract.yaml + generated main.bicepparam + policy XML
    contracts/backend/<env>/           the single authoritative backend contract per environment
    contracts/publish/<name>/<env>/
    contracts/alerts/<scope>/<env>/
    .citadel/admin-policy.yaml         admin policy (protected by CODEOWNERS)
    .citadel/rotations/                durable key-rotation transactions
    .citadel/locks/                    change locks per contract
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from citadel_agent.contracts.intent_io import INTENT_FILE_NAME, ContractIntent, load_intent
from citadel_agent.paths import DEFAULT_UPSTREAM_SUBMODULE
from citadel_agent.proc import run_command

CITADEL_DIR = PurePosixPath(".citadel")
ADMIN_POLICY_PATH = CITADEL_DIR / "admin-policy.yaml"
ROTATIONS_DIR = CITADEL_DIR / "rotations"
LOCKS_DIR = CITADEL_DIR / "locks"
CONTRACTS_DIR = PurePosixPath("contracts")
CHANGESET_MANIFEST = "changeset.json"


class RepositoryError(RuntimeError):
    """Raised when the canonical repository is missing or malformed."""


@dataclass(frozen=True)
class IntentRecord:
    """An intent loaded from the repository with its location."""

    path: PurePosixPath
    intent: ContractIntent


class CanonicalRepository:
    """Read and inspect a local checkout of the canonical contracts repository."""

    def __init__(self, root: Path, *, upstream_path: str = DEFAULT_UPSTREAM_SUBMODULE) -> None:
        self.root = root.resolve()
        self.upstream_path = PurePosixPath(upstream_path)

    # Locations -------------------------------------------------------------------------------
    def path(self, relative: PurePosixPath | str) -> Path:
        return self.root / Path(*PurePosixPath(relative).parts)

    @property
    def upstream_root(self) -> Path:
        return self.path(self.upstream_path)

    @property
    def admin_policy_file(self) -> Path:
        return self.path(ADMIN_POLICY_PATH)

    def validate_layout(self) -> list[str]:
        """Return problems with the repository layout (empty when usable)."""
        problems = []
        if not (self.root / ".git").exists():
            problems.append(f"{self.root} is not a git repository")
        if not (self.upstream_root / "release.json").is_file():
            problems.append(
                f"Upstream submodule missing at {self.upstream_path} (run git submodule update --init)"
            )
        if not self.admin_policy_file.is_file():
            problems.append(f"Admin policy missing at {ADMIN_POLICY_PATH}")
        return problems

    # Intents ---------------------------------------------------------------------------------
    def iter_intent_files(self) -> Iterator[PurePosixPath]:
        contracts = self.path(CONTRACTS_DIR)
        if not contracts.is_dir():
            return
        for file in sorted(contracts.rglob(INTENT_FILE_NAME)):
            yield PurePosixPath(file.relative_to(self.root).as_posix())

    def load(self, relative: PurePosixPath | str) -> ContractIntent:
        return load_intent(self.path(relative))

    def intents(self, kind: str | None = None) -> list[IntentRecord]:
        """Load every intent (optionally of one ``kind``). Invalid files raise, so problems surface early."""
        records = []
        for relative in self.iter_intent_files():
            intent = self.load(relative)
            if kind is None or intent.kind == kind:
                records.append(IntentRecord(path=relative, intent=intent))
        return records

    def find(self, relative_dir: PurePosixPath) -> ContractIntent | None:
        """Load the intent in ``relative_dir`` if it exists."""
        candidate = relative_dir / INTENT_FILE_NAME
        return self.load(candidate) if self.path(candidate).is_file() else None

    # Git state -------------------------------------------------------------------------------
    def _git(self, *args: str) -> str:
        return run_command("git", args, cwd=self.root).stdout.strip()

    def head_commit(self) -> str:
        return self._git("rev-parse", "HEAD")

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD")

    def upstream_commit(self) -> str:
        return run_command("git", ["rev-parse", "HEAD"], cwd=self.upstream_root).stdout.strip()

    def is_clean(self) -> bool:
        return self._git("status", "--porcelain") == ""

    def changed_paths(self) -> list[str]:
        lines = self._git("status", "--porcelain").splitlines()
        return [line[3:].strip() for line in lines if line.strip()]
