# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the allowlisted subprocess runner."""

from __future__ import annotations

import pytest

from citadel_agent import proc


def test_rejects_executable_not_on_allowlist() -> None:
    with pytest.raises(proc.CommandNotAllowedError):
        proc.run_command("python", ["--version"])


def test_runs_allowlisted_executable() -> None:
    result = proc.run_command("git", ["--version"])
    assert result.returncode == 0
    assert "git version" in result.stdout


def test_non_zero_exit_raises_command_error() -> None:
    with pytest.raises(proc.CommandError) as excinfo:
        proc.run_command("git", ["definitely-not-a-git-command"])
    assert excinfo.value.returncode not in (None, 0)


def test_non_zero_exit_without_check_returns_result() -> None:
    result = proc.run_command("git", ["definitely-not-a-git-command"], check=False)
    assert result.returncode != 0


@pytest.mark.parametrize("argument", ["rg&calc", "a|b", "100%", "x^y", 'quote"d', "line\nbreak"])
def test_batch_file_arguments_with_cmd_metacharacters_are_rejected(
    monkeypatch: pytest.MonkeyPatch, argument: str
) -> None:
    monkeypatch.setattr(proc.sys, "platform", "win32")
    monkeypatch.setattr(proc.shutil, "which", lambda name: r"C:\tools\az.cmd")
    with pytest.raises(proc.CommandNotAllowedError):
        proc.run_command("az", ["group", "show", "--name", argument])


def test_redact_is_applied_to_error_output(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(proc.CommandError) as excinfo:
        proc.run_command(
            "git",
            ["definitely-not-a-git-command"],
            redact=lambda text: text.replace("definitely-not-a-git-command", "***"),
        )
    assert "definitely-not-a-git-command" not in str(excinfo.value)
