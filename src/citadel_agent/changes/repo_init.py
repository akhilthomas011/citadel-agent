# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Create a canonical contracts repository from the template, pinned to the agent's tested upstream."""

from __future__ import annotations

import shutil
from pathlib import Path

from citadel_agent.changes.repository import ADMIN_POLICY_PATH, CanonicalRepository
from citadel_agent.changes.workspace import AGENT_GIT_EMAIL, AGENT_GIT_NAME
from citadel_agent.policy.admin import parse_admin_policy
from citadel_agent.proc import run_command
from citadel_agent.upstream.lock import UpstreamLock


class RepoInitError(RuntimeError):
    """Raised when the repository cannot be initialized."""


def _git(cwd: Path, *args: str) -> str:
    return run_command("git", args, cwd=cwd, timeout=1800).stdout.strip()


def init_canonical_repository(
    path: Path,
    *,
    template_dir: Path,
    lock: UpstreamLock,
    upstream_source: str,
    admin_policy_text: str,
) -> CanonicalRepository:
    """Initialize a new canonical repository at ``path``.

    Args:
        path: Empty or missing directory for the new repository.
        template_dir: The ``templates/contracts-repo`` folder of the agent.
        lock: The agent's upstream pin; the repository is pinned to the same commit.
        upstream_source: Where to clone the upstream from (a local path is fastest); the submodule
            URL is then set to ``lock.upstream.repository``.
        admin_policy_text: The admin policy YAML to commit (validated first).
    """
    parse_admin_policy(admin_policy_text)  # fail before creating anything
    if path.exists() and any(path.iterdir()):
        raise RepoInitError(f"{path} is not empty")
    shutil.copytree(template_dir, path, dirs_exist_ok=True)
    (path / ADMIN_POLICY_PATH.as_posix()).write_text(admin_policy_text, encoding="utf-8", newline="\n")
    _git(path, "init", "-b", "main")
    submodule = lock.upstream.path
    _git(
        path,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-b",
        lock.upstream.branch,
        upstream_source,
        submodule,
    )
    _git(path / submodule, "checkout", "--detach", lock.upstream.commit)
    _git(path, "config", "-f", ".gitmodules", f"submodule.{submodule}.url", lock.upstream.repository)
    _git(path, "submodule", "sync")
    _git(path, "add", "-A")
    name = run_command("git", ["config", "user.name"], cwd=path, check=False).stdout.strip() or AGENT_GIT_NAME
    email = (
        run_command("git", ["config", "user.email"], cwd=path, check=False).stdout.strip() or AGENT_GIT_EMAIL
    )
    _git(
        path,
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        "-m",
        "Initialize canonical Citadel contracts repository",
    )
    repo = CanonicalRepository(path, upstream_path=submodule)
    if repo.upstream_commit() != lock.upstream.commit:
        raise RepoInitError("The upstream submodule is not at the agent's pinned commit")
    allow_recording_pushes(path)
    return repo


def allow_recording_pushes(path: Path) -> None:
    """Let the agent's working clone fast-forward this local repository's checked-out branch.

    With ``updateInstead`` git updates the working tree on push, and refuses the push when the
    working tree has uncommitted changes, so local edits are never overwritten.
    """
    _git(path, "config", "receive.denyCurrentBranch", "updateInstead")
