# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Well-known filesystem locations for the Citadel Agent.

Wheels contain a read-only runtime asset tree. Source checkouts use their pinned upstream
submodule instead. Set ``CITADEL_AGENT_HOME`` to explicitly select a checkout.
"""

from __future__ import annotations

import os
from pathlib import Path

LOCK_FILE_NAME = "citadel.lock.json"
DEFAULT_UPSTREAM_SUBMODULE = "upstream/ai-hub-gateway"
STATE_DIR_NAME = ".citadel-agent"
ENV_HOME = "CITADEL_AGENT_HOME"
BUNDLED_ROOT = Path(__file__).resolve().parent / "_assets"


class ProjectRootNotFoundError(RuntimeError):
    """Raised when the agent checkout (with its pinned upstream) cannot be located."""


class CheckoutRequiredError(ProjectRootNotFoundError):
    """Raised for git operations unavailable with read-only packaged assets."""


def is_bundled_root(root: Path) -> bool:
    """Whether ``root`` is the installed package's immutable asset tree."""
    return root.resolve() == BUNDLED_ROOT.resolve()


def require_checkout(root: Path) -> Path:
    """Reject checkout-only operations before they can mutate installed package data."""
    has_git = (root / ".git").exists() or (root / DEFAULT_UPSTREAM_SUBMODULE / ".git").exists()
    if is_bundled_root(root) or not has_git:
        raise CheckoutRequiredError(
            "Upstream git operations are checkout-only. Clone the agent repository with its "
            f"submodules and set {ENV_HOME} to that checkout; bundled assets are read-only."
        )
    return root


def _is_project_root(candidate: Path) -> bool:
    if (candidate / LOCK_FILE_NAME).is_file():
        return True
    return (candidate / "pyproject.toml").is_file() and (candidate / DEFAULT_UPSTREAM_SUBMODULE).is_dir()


def find_project_root(start: Path | None = None) -> Path:
    """Locate the agent checkout root.

    Resolution order: explicit ``CITADEL_AGENT_HOME``, installed assets, then checkout discovery.
    Installed packages never trust an arbitrary current directory's lock file or skills.

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

    if (BUNDLED_ROOT / LOCK_FILE_NAME).is_file():
        return BUNDLED_ROOT

    bases = [(start or Path.cwd()).resolve(), Path(__file__).resolve().parent]
    for base in bases:
        for candidate in (base, *base.parents):
            if _is_project_root(candidate):
                return candidate
    raise ProjectRootNotFoundError(
        f"Could not find Citadel Agent assets. Reinstall the package or set {ENV_HOME} to a checkout."
    )


def lock_file_path(root: Path) -> Path:
    """Return the path of ``citadel.lock.json`` under ``root``."""
    return root / LOCK_FILE_NAME


def state_dir(root: Path) -> Path:
    """Return writable state, never inside an installed package's asset tree."""
    override = os.environ.get("CITADEL_AGENT_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if is_bundled_root(root):
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        else:
            base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        return base / "citadel-agent"
    return root / STATE_DIR_NAME
