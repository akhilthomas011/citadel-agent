# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""The agent's own working clone of the canonical contracts repository.

The agent never edits a user's working copy. It keeps a dedicated clone (by default under
``.citadel-agent/repo``) of the canonical repository (a hosted git URL, or a local path for
local-only development), resets it to the base branch before every change and works on one branch
per change set.
"""

from __future__ import annotations

import re
from pathlib import Path

from citadel_agent.changes.repository import CanonicalRepository
from citadel_agent.paths import DEFAULT_UPSTREAM_SUBMODULE
from citadel_agent.proc import CommandError, run_command

AGENT_GIT_NAME = "Citadel Agent"
AGENT_GIT_EMAIL = "citadel-agent@users.noreply.github.com"

_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SCP_LIKE = re.compile(r"^[\w.-]+@[\w.-]+:")


class WorkspaceError(RuntimeError):
    """Raised when the working clone cannot be prepared."""


def is_local_location(url: str) -> bool:
    """True when ``url`` is a filesystem path (or ``file://`` URL) rather than a hosted remote."""
    if url.startswith("file://"):
        return True
    return not (_URL_SCHEME.match(url) or _SCP_LIKE.match(url))


def _normalize_location(url: str) -> str:
    if is_local_location(url):
        path = url.removeprefix("file://")
        return str(Path(path).resolve()).casefold()
    return url.rstrip("/").removesuffix(".git").casefold()


class CanonicalWorkspace:
    def __init__(
        self,
        path: Path,
        *,
        remote_url: str | None = None,
        base_branch: str = "main",
        upstream_path: str = DEFAULT_UPSTREAM_SUBMODULE,
    ) -> None:
        self.path = path.resolve()
        self.remote_url = remote_url
        self.base_branch = base_branch
        self.upstream_path = upstream_path

    @property
    def repository(self) -> CanonicalRepository:
        return CanonicalRepository(self.path, upstream_path=self.upstream_path)

    def _git(self, *args: str, check: bool = True) -> str:
        return run_command("git", args, cwd=self.path, check=check, timeout=900).stdout.strip()

    def has_remote(self) -> bool:
        result = run_command("git", ["remote"], cwd=self.path, check=False)
        return "origin" in result.stdout.split()

    def origin_url(self) -> str | None:
        result = run_command("git", ["remote", "get-url", "origin"], cwd=self.path, check=False)
        return (result.stdout.strip() or None) if result.returncode == 0 else None

    def remote_is_local(self) -> bool:
        """True when the canonical repository is a local repository (no hosting provider)."""
        url = self.origin_url()
        return url is not None and is_local_location(url)

    @property
    def base_ref(self) -> str:
        return f"origin/{self.base_branch}" if self.has_remote() else self.base_branch

    def ensure(self) -> None:
        """Clone the canonical repository if needed (with a shallow upstream submodule)."""
        if (self.path / ".git").exists():
            origin = self.origin_url()
            if (
                self.remote_url
                and origin
                and _normalize_location(origin) != _normalize_location(self.remote_url)
            ):
                raise WorkspaceError(
                    f"The agent's working clone at {self.path} tracks {origin}, not {self.remote_url}. "
                    "Remove that folder so the agent can clone the configured repository."
                )
            return
        if not self.remote_url:
            raise WorkspaceError(
                "No canonical contracts repository is configured. Set CITADEL_AGENT_REPO_URL to its git "
                "URL (or to a local path for local-only development)."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            run_command(
                "git",
                ["clone", "--branch", self.base_branch, self.remote_url, str(self.path)],
                cwd=self.path.parent,
                timeout=1800,
            )
            self._update_submodules()
        except CommandError as exc:
            raise WorkspaceError(f"Could not clone {self.remote_url}: {exc}") from exc

    def _update_submodules(self) -> None:
        self._git("submodule", "update", "--init", "--recursive", "--depth", "1")

    def sync_base(self) -> str:
        """Reset the clone to the latest base branch and return its commit."""
        self.ensure()
        if self.has_remote():
            self._git("fetch", "origin", self.base_branch, "--prune")
        self._git("checkout", "--force", self.base_branch)
        self._git("reset", "--hard", self.base_ref)
        self._git("clean", "-fd")
        self._update_submodules()
        return self._git("rev-parse", "HEAD")

    def base_commit(self) -> str:
        """The current commit of the base branch (after fetching when a remote exists)."""
        if self.has_remote():
            self._git("fetch", "origin", self.base_branch)
        return self._git("rev-parse", self.base_ref)

    def begin_branch(self, branch: str) -> None:
        """Create (or reset) ``branch`` at the base branch and check it out."""
        self._git("checkout", "--force", "-B", branch, self.base_ref)
        self._update_submodules()

    def write(self, files: dict[str, str]) -> None:
        for relative, content in files.items():
            target = self.path / Path(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")

    def read(self, relative: str) -> str | None:
        target = self.path / Path(*relative.split("/"))
        return target.read_text(encoding="utf-8") if target.is_file() else None

    def commit(self, message: str, paths: list[str], *, author: str | None = None) -> str:
        """Stage ``paths`` and commit. Uses the user's git identity, or the agent identity when unset."""
        self._git("add", "--", *paths)
        name = self._git("config", "user.name", check=False) or AGENT_GIT_NAME
        email = self._git("config", "user.email", check=False) or AGENT_GIT_EMAIL
        args = ["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", message]
        if author:
            args.extend(["-m", f"Requested-by: {author}"])
        self._git(*args)
        return self._git("rev-parse", "HEAD")

    def push(self, branch: str) -> None:
        if not self.has_remote():
            raise WorkspaceError("The canonical repository has no remote to push to")
        self._git("push", "--force-with-lease", "-u", "origin", branch)

    def push_base(self) -> None:
        """Fast-forward the remote base branch to HEAD (used for local canonical repositories).

        Never forces. A non-bare local repository must allow it with
        ``receive.denyCurrentBranch=updateInstead`` (set by ``citadel-agent repo init``); git then
        refuses the push when that working tree has uncommitted changes.
        """
        self._git("push", "origin", f"HEAD:refs/heads/{self.base_branch}")

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD")
