# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Minimal semantic version parsing for upstream release tracks (for example ``1.1.0-preview``)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


class InvalidVersionError(ValueError):
    """Raised when a string is not a valid semantic version."""


@total_ordering
@dataclass(frozen=True)
class SemVer:
    """A parsed semantic version. Build metadata is ignored for comparisons."""

    major: int
    minor: int
    patch: int
    prerelease: str = ""

    @classmethod
    def parse(cls, value: str) -> SemVer:
        match = _SEMVER.match(value.strip())
        if not match:
            raise InvalidVersionError(f"Not a semantic version: {value!r}")
        return cls(
            major=int(match["major"]),
            minor=int(match["minor"]),
            patch=int(match["patch"]),
            prerelease=match["prerelease"] or "",
        )

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def _key(self) -> tuple[int, int, int, int, str]:
        # A release sorts after its pre-releases (1.0.0-preview < 1.0.0).
        return (self.major, self.minor, self.patch, 0 if self.prerelease else 1, self.prerelease)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._key() < other._key()

    def __str__(self) -> str:
        suffix = f"-{self.prerelease}" if self.prerelease else ""
        return f"{self.major}.{self.minor}.{self.patch}{suffix}"
