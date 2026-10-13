# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for citadel.lock.json handling."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from citadel_agent.upstream import lock as lockmod

RELEASE = {"master-version": "1.0.2", "access-contract-version": "1.2.0"}
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def _make_checkout(root: Path, release: dict[str, str] | None = None) -> None:
    upstream = root / "upstream" / "ai-hub-gateway"
    upstream.mkdir(parents=True)
    (upstream / "release.json").write_text(json.dumps(release or RELEASE), encoding="utf-8")


def _patch_git(monkeypatch: pytest.MonkeyPatch, commit: str) -> None:
    monkeypatch.setattr(
        lockmod, "submodule_settings", lambda *args, **kwargs: ("https://example.test/repo.git", "citadel-v1")
    )
    monkeypatch.setattr(lockmod, "submodule_commit", lambda *args, **kwargs: commit)


def test_build_write_read_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_checkout(tmp_path)
    _patch_git(monkeypatch, COMMIT_A)

    built = lockmod.build_lock(tmp_path, now=datetime(2026, 1, 1, tzinfo=UTC))
    lockmod.write_lock(tmp_path, built)
    loaded = lockmod.read_lock(tmp_path)

    assert loaded == built
    assert loaded.upstream.commit == COMMIT_A
    assert loaded.upstream.release["access-contract-version"] == "1.2.0"


def test_check_lock_reports_in_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_checkout(tmp_path)
    _patch_git(monkeypatch, COMMIT_A)
    lockmod.write_lock(tmp_path, lockmod.build_lock(tmp_path))

    assert lockmod.check_lock(tmp_path).in_sync


def test_check_lock_detects_commit_and_release_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_checkout(tmp_path)
    _patch_git(monkeypatch, COMMIT_A)
    lockmod.write_lock(tmp_path, lockmod.build_lock(tmp_path))

    _patch_git(monkeypatch, COMMIT_B)
    release_file = tmp_path / "upstream" / "ai-hub-gateway" / "release.json"
    release_file.write_text(json.dumps({**RELEASE, "access-contract-version": "2.0.0"}), encoding="utf-8")

    status = lockmod.check_lock(tmp_path)
    assert not status.commit_matches
    assert not status.release_matches
    assert not status.in_sync


def test_pin_rejects_invalid_commit() -> None:
    with pytest.raises(ValidationError):
        lockmod.UpstreamPin(repository="r", branch="b", commit="not-a-sha", path="p", release={})
