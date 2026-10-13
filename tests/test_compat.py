# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for release-track compatibility assessment."""

from __future__ import annotations

import pytest

from citadel_agent.upstream.compat import CompatLevel, assess_release

TESTED = {
    "master-version": "1.0.2",
    "routing-version": "1.0.0",
    "backend-contract-version": "1.1.0",
    "access-contract-version": "1.2.0",
    "publish-contract-version": "1.1.0-preview",
}


def test_identical_release_is_ok_everywhere() -> None:
    report = assess_release(dict(TESTED), TESTED)
    assert report.overall is CompatLevel.OK
    for capability in ("access", "backend", "publish", "alerts"):
        assert report.writes_allowed(capability)


def test_unknown_live_release_blocks_all_writes() -> None:
    report = assess_release(None, TESTED)
    assert report.overall is CompatLevel.BLOCK_WRITES
    assert not report.writes_allowed("access")


@pytest.mark.parametrize(
    ("live_version", "expected"),
    [
        ("1.2.5", CompatLevel.WARN),  # patch drift
        ("1.3.0", CompatLevel.WARN),  # newer minor
        ("1.1.9", CompatLevel.BLOCK_WRITES),  # older minor
        ("2.0.0", CompatLevel.BLOCK_WRITES),  # different major
        ("not-a-version", CompatLevel.BLOCK_WRITES),
    ],
)
def test_access_contract_track_rules(live_version: str, expected: CompatLevel) -> None:
    report = assess_release({**TESTED, "access-contract-version": live_version}, TESTED)
    assert report.level_for("access") is expected


def test_backend_track_requires_exact_match() -> None:
    report = assess_release({**TESTED, "backend-contract-version": "1.1.1"}, TESTED)
    assert report.level_for("backend") is CompatLevel.BLOCK_WRITES
    assert report.writes_allowed("access")


def test_preview_track_requires_exact_match() -> None:
    report = assess_release({**TESTED, "publish-contract-version": "1.2.0-preview"}, TESTED)
    assert report.level_for("publish") is CompatLevel.BLOCK_WRITES


def test_missing_live_track_blocks_its_capability() -> None:
    live = {k: v for k, v in TESTED.items() if k != "access-contract-version"}
    report = assess_release(live, TESTED)
    assert not report.writes_allowed("access")
    assert report.writes_allowed("backend")


def test_unknown_live_track_is_a_warning() -> None:
    report = assess_release({**TESTED, "new-track-version": "1.0.0"}, TESTED)
    assert report.assessments["new-track-version"].level is CompatLevel.WARN
    assert report.writes_allowed("access")


def test_unknown_capability_raises() -> None:
    report = assess_release(dict(TESTED), TESTED)
    with pytest.raises(KeyError):
        report.level_for("unknown")
