# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for checkout-root discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from citadel_agent import paths


def test_env_var_overrides_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
    assert paths.find_project_root() == tmp_path.resolve()


def test_env_var_pointing_to_missing_directory_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "missing"))
    with pytest.raises(paths.ProjectRootNotFoundError):
        paths.find_project_root()


def test_discovers_root_from_nested_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.ENV_HOME, raising=False)
    (tmp_path / paths.LOCK_FILE_NAME).write_text("{}", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert paths.find_project_root(nested) == tmp_path.resolve()


def test_installed_assets_are_preferred_to_untrusted_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "installed" / "_assets"
    bundle.mkdir(parents=True)
    (bundle / paths.LOCK_FILE_NAME).write_text("{}", encoding="utf-8")
    (tmp_path / paths.LOCK_FILE_NAME).write_text("{}", encoding="utf-8")
    monkeypatch.delenv(paths.ENV_HOME, raising=False)
    monkeypatch.setattr(paths, "BUNDLED_ROOT", bundle)
    assert paths.find_project_root(tmp_path) == bundle


def test_installed_state_is_outside_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "site-packages" / "_assets"
    monkeypatch.setattr(paths, "BUNDLED_ROOT", bundle)
    monkeypatch.delenv("CITADEL_AGENT_STATE_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "user"))
    assert paths.state_dir(bundle) == tmp_path / "user" / "citadel-agent"
    assert paths.state_dir(tmp_path) == tmp_path / paths.STATE_DIR_NAME


def test_state_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CITADEL_AGENT_STATE_DIR", str(tmp_path / "state"))
    assert paths.state_dir(tmp_path) == tmp_path / "state"


def test_require_checkout_rejects_bundle_and_non_git_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "BUNDLED_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    with pytest.raises(paths.CheckoutRequiredError, match="checkout-only"):
        paths.require_checkout(tmp_path)
    with pytest.raises(paths.CheckoutRequiredError, match="checkout-only"):
        paths.require_checkout(tmp_path / "not-a-checkout")
