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
