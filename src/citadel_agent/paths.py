# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Well-known filesystem locations for the Citadel Agent.

The agent runs from a checkout that contains the pinned upstream Citadel repository as a git
submodule and a ``citadel.lock.json`` file describing that pin. Set ``CITADEL_AGENT_HOME`` to
point the agent at a checkout when running from elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

LOCK_FILE_NAME = "citadel.lock.json"
DEFAULT_UPSTREAM_SUBMODULE = "upstream/ai-hub-gateway"
STATE_DIR_NAME = ".citadel-agent"
ENV_HOME = "CITADEL_AGENT_HOME"


class ProjectRootNotFoundError(RuntimeError):
    """Raised when the agent checkout (with its pinned upstream) cannot be located."""


def _is_project_root(candidate: Path) -> bool:
    if (candidate / LOCK_FILE_NAME).is_file():
        return True
    return (candidate / "pyproject.toml").is_file() and (candidate / DEFAULT_UPSTREAM_SUBMODULE).is_dir()


def find_project_root(start: Path | None = None) -> Path:
    """Locate the agent checkout root.

    Resolution order: the ``CITADEL_AGENT_HOME`` environment variable, then the directories above
    ``start`` (default: current working directory), then the directories above this module.

    Args:
        start: Directory to start searching from.

    Returns:
        Absolute path of the checkout root.

    Raises:
        ProjectRootNotFoundError: If no checkout root can be found.
    """
    env_home = os.environ.get(ENV_HOME)
    if env_home:
        root = Path(env_home).expanduser().resolve()
        if not root.is_dir():
            raise ProjectRootNotFoundError(f"{ENV_HOME} points to a missing directory: {root}")
        return root

    bases = [(start or Path.cwd()).resolve(), Path(__file__).resolve().parent]
    for base in bases:
        for candidate in (base, *base.parents):
            if _is_project_root(candidate):
                return candidate
    raise ProjectRootNotFoundError(
        f"Could not find the Citadel Agent checkout. Run from the repository or set {ENV_HOME}."
    )


def lock_file_path(root: Path) -> Path:
    """Return the path of ``citadel.lock.json`` under ``root``."""
    return root / LOCK_FILE_NAME


def state_dir(root: Path) -> Path:
    """Return the local runtime state directory (drafts, change sets, audit log) under ``root``."""
    return root / STATE_DIR_NAME
