# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for semantic version parsing."""

from __future__ import annotations

import pytest

from citadel_agent.upstream.semver import InvalidVersionError, SemVer


def test_parse_release_and_prerelease() -> None:
    assert SemVer.parse("1.2.3") == SemVer(1, 2, 3)
    preview = SemVer.parse("1.1.0-preview")
    assert preview.prerelease == "preview"
    assert preview.is_prerelease
    assert str(preview) == "1.1.0-preview"


def test_ordering_places_prerelease_before_release() -> None:
    assert SemVer.parse("1.0.0-preview") < SemVer.parse("1.0.0")
    assert SemVer.parse("1.0.2") < SemVer.parse("1.1.0") < SemVer.parse("2.0.0")


def test_build_metadata_is_ignored() -> None:
    assert SemVer.parse("1.0.0+build.5") == SemVer.parse("1.0.0")


@pytest.mark.parametrize("value", ["1.0", "v1.0.0", "01.0.0", "1.0.0-", ""])
def test_invalid_versions_are_rejected(value: str) -> None:
    with pytest.raises(InvalidVersionError):
        SemVer.parse(value)
