# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Validate wheel asset shape and provenance: python scripts\\check_wheel.py <wheel>."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath


def check_wheel(path: Path) -> None:
    prefix = "citadel_agent/_assets/"
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        assert len(names) == len(set(names)), "Duplicate wheel members"
        assert all(
            not {".git", ".env", ".venv", "__pycache__", ".citadel-agent"}.intersection(
                PurePosixPath(name).parts
            )
            for name in names
        ), "Development state or secrets directory in wheel"
        manifest = json.loads(wheel.read(prefix + "manifest.json"))
        assets = {
            name.removeprefix(prefix): hashlib.sha256(wheel.read(name)).hexdigest()
            for name in names
            if name.startswith(prefix) and name != prefix + "manifest.json" and not name.endswith("/")
        }
        assert assets == manifest, "Asset manifest mismatch"
        lock = json.loads(wheel.read(prefix + "citadel.lock.json"))
        upstream = lock["upstream"]["path"] + "/"
        assert json.loads(wheel.read(prefix + upstream + "release.json")) == lock["upstream"]["release"]
        required = {
            "config/admin-policy.example.yaml",
            "templates/contracts-repo/.citadel/admin-policy.yaml",
            "templates/contracts-repo/.gitignore",
            "skills/citadel-overview/SKILL.md",
            upstream + "LICENSE",
            upstream + "bicep/infra/citadel-access-contracts/main.bicep",
            upstream + "src/usage-reports/model-pricing-generated-extended.json",
        }
        assert required <= assets.keys(), f"Missing runtime assets: {required - assets.keys()}"
        assert any(name.startswith("citadel_agent/knowledge/data/") for name in names)
        assert any(".dist-info/licenses/LICENSE" in name for name in names), "Agent license is missing"
        assert all(name.startswith("citadel_agent/") or ".dist-info/" in name for name in names)
        print(f"Wheel shape passed: {len(assets)} assets, upstream {lock['upstream']['commit']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    check_wheel(parser.parse_args().wheel)
