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


@pytest.mark.parametrize(
    "command", [["doctor"], ["repo", "init", "--path", "x", "--policy", "y.yaml"], ["chat"]]
)
def test_commands_outside_a_checkout_fail_cleanly_instead_of_crashing(
    command: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from citadel_agent.cli import main

    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "missing"))
    monkeypatch.setattr("sys.argv", ["citadel-agent", *command])

    with pytest.raises(SystemExit) as raised:
        main()

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "CITADEL_AGENT_HOME" in captured.err or "CITADEL_AGENT_HOME" in captured.out
    assert "Traceback" not in captured.err + captured.out
