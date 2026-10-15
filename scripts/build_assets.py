# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Hatch hook: build a curated runtime tree from trusted files and the exact upstream pin.

An sdist carries the same tree, so rebuilding its wheel needs neither Git nor a checkout.
No working-tree upstream content, git metadata, or untracked local configuration is shipped.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def local_asset(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if any(part in {".git", ".env", "__pycache__", "node_modules", ".venv"} for part in parts):
        return False
    return (
        path == "config/admin-policy.example.yaml"
        or (parts[0] == "skills" and path.endswith(".md"))
        or parts[0] == "templates"
    )


def git(root: Path, *args: str) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("Git is required when building from a source checkout (not from an sdist)")
    return subprocess.run(  # noqa: S603
        [executable, "-C", str(root), *args], capture_output=True, check=True
    ).stdout


def upstream_asset(path: str) -> bool:
    """Include the full infrastructure dependency tree, docs, pricing data and license."""
    parts = PurePosixPath(path).parts
    if any(part in {".git", ".env", "__pycache__", "node_modules", ".venv"} for part in parts):
        return False
    return (
        path in {"LICENSE", "release.json", "src/usage-reports/model-pricing-generated-extended.json"}
        or parts[0] in {"bicep", "shared"}
        or (path.endswith(".md") and parts[0] != "assets")
    )


def build_assets(root: Path, destination: Path) -> None:
    lock = json.loads((root / "citadel.lock.json").read_text(encoding="utf-8"))
    pin = lock["upstream"]
    upstream = root / pin["path"]
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    (destination / "citadel.lock.json").write_bytes((root / "citadel.lock.json").read_bytes())
    tracked = git(root, "ls-files", "-z", "--", "skills", "templates", "config").decode().split("\0")
    for name in filter(None, tracked):
        if not local_asset(name):
            continue
        source = root / name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Runtime asset must be a regular tracked file: {name}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    archive = git(upstream, "archive", "--format=zip", pin["commit"])
    with zipfile.ZipFile(io.BytesIO(archive)) as pinned:
        for entry in pinned.infolist():
            relative = entry.filename
            if entry.is_dir() or not upstream_asset(relative):
                continue
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"Upstream asset must be a regular file: {relative}")
            target = destination / pin["path"] / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(pinned.read(entry))
    release = json.loads((destination / pin["path"] / "release.json").read_text(encoding="utf-8"))
    if release != pin["release"]:
        raise ValueError("Pinned upstream release does not match citadel.lock.json")
    if not (destination / pin["path"] / "LICENSE").is_file():
        raise ValueError("Pinned upstream license is required")
    manifest = {
        path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        assets = root / "src" / "citadel_agent" / "_assets"
        if assets.is_dir():
            return  # Already part of the module tree in an sdist.
        assets = root / "build" / "runtime-assets"
        build_assets(root, assets)
        target = "citadel_agent/_assets" if self.target_name == "wheel" else "src/citadel_agent/_assets"
        build_data.setdefault("force_include", {})[str(assets)] = target
