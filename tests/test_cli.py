# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the command-line interface."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from citadel_agent import paths
from citadel_agent.cli import app
from citadel_agent.upstream import UpstreamLock, UpstreamPin, write_lock

runner = CliRunner()


def _write_lock(root: Path) -> None:
    write_lock(
        root,
        UpstreamLock(
            upstream=UpstreamPin(
                repository="https://example.test/repo.git",
                branch="citadel-v1",
                commit="c" * 40,
                path="upstream/ai-hub-gateway",
                release={"master-version": "1.0.2"},
            ),
            synced_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )


def test_version_shows_pinned_upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_lock(tmp_path)
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert "citadel-v1" in result.output
    assert "master-version" in result.output


def test_version_without_lock_is_a_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 2
