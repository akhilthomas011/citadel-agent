# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Offline installed-wheel smoke test, run from an empty directory with the installed Python.

Example (after building and installing the wheel plus its devui extra in a fresh venv):
    <installed-python> -I <checkout>\\scripts\\smoke_installed.py

Identity and network boundaries are mocked, not asset loading, runtime, agent construction,
CLI chat initialization, or DevUI initialization. --repo-init also clones the real remote pin.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

from azure.core.credentials import AccessToken
from typer.testing import CliRunner

from citadel_agent import paths
from citadel_agent.cli import app
from citadel_agent.knowledge.catalogs import catalog_drift, load_catalogs
from citadel_agent.knowledge.docs import build_doc_index
from citadel_agent.pricing import sample_file
from citadel_agent.runtime import build_runtime
from citadel_agent.settings import Settings
from citadel_agent.upstream.layout import locate_upstream
from citadel_agent.upstream.lock import read_lock


class OfflineCredential:
    def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        claims = (
            base64.urlsafe_b64encode(
                json.dumps({"oid": "smoke", "tid": "offline", "upn": "smoke@example.test"}).encode()
            )
            .decode()
            .rstrip("=")
        )
        return AccessToken(f"header.{claims}.signature", 4_000_000_000)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-init", action="store_true", help="Clone the real remote (requires Git/network)"
    )
    args = parser.parse_args()
    if os.environ.get(paths.ENV_HOME):
        raise AssertionError(f"Unset {paths.ENV_HOME} for an installed-wheel smoke test")
    root = paths.find_project_root()
    assert paths.is_bundled_root(root), f"Not running the installed wheel: {root}"
    assert not (Path.cwd() / "pyproject.toml").exists(), "Run from an empty directory outside the checkout"
    assert not (root / ".git").exists()
    assert (root / "config" / "admin-policy.example.yaml").is_file()
    assert (root / "templates" / "contracts-repo" / ".gitignore").is_file()
    lock = read_lock(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    actual = {
        file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in root.rglob("*")
        if file.is_file() and file.name != "manifest.json"
    }
    assert actual == manifest, "Bundled asset manifest does not match installed files"
    assert all(".git" not in Path(name).parts and ".env" not in Path(name).parts for name in actual)
    layout = locate_upstream()
    assert not layout.missing_files(), layout.missing_files()
    assert (layout.root / "LICENSE").is_file()
    assert sample_file(layout.root).is_file()
    index = build_doc_index(layout.root)
    assert index.sections
    assert not catalog_drift(load_catalogs(), layout.root, index)
    default_state = paths.state_dir(root)
    assert not default_state.is_relative_to(root.parent)
    state = Path.cwd() / "state"
    os.environ["CITADEL_AGENT_STATE_DIR"] = str(state)
    assert paths.state_dir(root) == state
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert lock.upstream.commit in result.output.replace("\n", "").replace(" ", "")
    for command in ("status", "lock", "sync"):
        result = runner.invoke(app, ["upstream", command])
        assert result.exit_code == 2 and "checkout-only" in result.output, result.output
    with patch("citadel_agent.runtime.create_credential", return_value=OfflineCredential()):
        runtime = build_runtime(Settings(_env_file=None))
        assert runtime.project_root == root
        assert runtime.store.directory.is_relative_to(state)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        # The real client and agent are constructed, but no model request is made.
        from agent_framework.openai import OpenAIChatCompletionClient

        client = OpenAIChatCompletionClient(
            model="smoke", api_key="not-a-secret", base_url="https://offline.example.test/v1"
        )
        with patch("citadel_agent.llm_client.create_chat_client", return_value=client):
            result = runner.invoke(app, ["chat"], input="/exit\n")
            assert result.exit_code == 0, result.output
            assert "Session" in result.output
            with patch("agent_framework.devui.serve") as serve:
                result = runner.invoke(app, ["devui", "--no-open"])
                assert result.exit_code == 0, result.output
                serve.assert_called_once()
    policy = root / "config" / "admin-policy.example.yaml"
    destination = Path.cwd() / "contracts"
    if args.repo_init:
        result = runner.invoke(app, ["repo", "init", "--path", str(destination), "--policy", str(policy)])
        assert result.exit_code == 0, result.output
    else:
        from types import SimpleNamespace

        with patch(
            "citadel_agent.cli_admin.init_canonical_repository",
            return_value=SimpleNamespace(root=destination),
        ) as initialize:
            result = runner.invoke(app, ["repo", "init", "--path", str(destination), "--policy", str(policy)])
            assert result.exit_code == 0, result.output
            assert initialize.call_args.kwargs["upstream_source"] == lock.upstream.repository
    after = {
        file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in root.rglob("*")
        if file.is_file() and file.name != "manifest.json"
    }
    assert after == manifest, "Runtime modified installed assets"
    print(f"Installed runtime smoke passed: {len(manifest)} assets, {len(index.sections)} doc sections")


if __name__ == "__main__":
    main()
