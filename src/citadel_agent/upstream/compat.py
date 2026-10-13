# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Compatibility between this agent build and the release tracks of a Citadel deployment.

The agent is tested against the upstream release recorded in ``citadel.lock.json``. Before it
changes anything, it compares those tested versions with the live gateway's ``GET /version``
response (or a contracts repository's upstream pin) and decides, per track, whether write
operations are allowed. Read-only assistance is always allowed.

Rules:

* exact match with a tested version: ``ok``
* backend-contract and pre-release (preview) tracks require an exact match, otherwise ``block-writes``
* same major and minor, different patch: ``warn``
* same major, newer minor than tested: ``warn`` (upstream treats MINOR as backward compatible)
* same major, older minor than tested: ``block-writes`` (the gateway may lack fragments the agent uses)
* different major, unknown or unparsable version: ``block-writes``
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from citadel_agent.upstream.semver import InvalidVersionError, SemVer

ACCESS_TRACK = "access-contract-version"
BACKEND_TRACK = "backend-contract-version"
PUBLISH_TRACK = "publish-contract-version"
MASTER_TRACK = "master-version"
ROUTING_TRACK = "routing-version"

# Contract capability -> release track that governs whether the agent may write it.
CAPABILITY_TRACKS: Mapping[str, str] = {
    "access": ACCESS_TRACK,
    "backend": BACKEND_TRACK,
    "publish": PUBLISH_TRACK,
    "alerts": MASTER_TRACK,
}

EXACT_MATCH_TRACKS = frozenset({BACKEND_TRACK})


class CompatLevel(StrEnum):
    """Outcome of a compatibility assessment, ordered from best to worst."""

    OK = "ok"
    WARN = "warn"
    BLOCK_WRITES = "block-writes"

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {CompatLevel.OK: 0, CompatLevel.WARN: 1, CompatLevel.BLOCK_WRITES: 2}


@dataclass(frozen=True)
class TrackPolicy:
    """Versions of one release track that this agent build was tested with."""

    tested: tuple[str, ...]
    exact: bool = False


@dataclass(frozen=True)
class TrackAssessment:
    """Compatibility verdict for one release track."""

    track: str
    live: str | None
    tested: tuple[str, ...]
    level: CompatLevel
    reason: str


@dataclass(frozen=True)
class CompatReport:
    """Compatibility verdicts for all release tracks."""

    assessments: Mapping[str, TrackAssessment] = field(default_factory=dict)

    @property
    def overall(self) -> CompatLevel:
        levels = [a.level for a in self.assessments.values()]
        return max(levels, key=lambda level: level.rank, default=CompatLevel.OK)

    def level_for(self, capability: str) -> CompatLevel:
        """Return the verdict that governs writes for a contract capability (for example ``access``)."""
        track = CAPABILITY_TRACKS.get(capability)
        if track is None:
            raise KeyError(f"Unknown capability: {capability}")
        assessment = self.assessments.get(track)
        return assessment.level if assessment else CompatLevel.BLOCK_WRITES

    def writes_allowed(self, capability: str) -> bool:
        return self.level_for(capability) is not CompatLevel.BLOCK_WRITES

    def problems(self) -> list[TrackAssessment]:
        """Return the assessments that are not ``ok``."""
        return [a for a in self.assessments.values() if a.level is not CompatLevel.OK]


def policies_from_release(tested_release: Mapping[str, str]) -> dict[str, TrackPolicy]:
    """Build track policies from the release tracks this agent build was tested with.

    Backend-contract and pre-release tracks require an exact match.
    """
    policies: dict[str, TrackPolicy] = {}
    for track, version in tested_release.items():
        try:
            prerelease = SemVer.parse(version).is_prerelease
        except InvalidVersionError:
            prerelease = True
        policies[track] = TrackPolicy(tested=(version,), exact=track in EXACT_MATCH_TRACKS or prerelease)
    return policies


def assess_track(track: str, live: str | None, policy: TrackPolicy) -> TrackAssessment:
    """Assess one release track of a deployment against the tested versions."""

    def verdict(level: CompatLevel, reason: str) -> TrackAssessment:
        return TrackAssessment(track=track, live=live, tested=policy.tested, level=level, reason=reason)

    if live is None:
        return verdict(CompatLevel.BLOCK_WRITES, "The deployment does not report this track.")
    if live in policy.tested:
        return verdict(CompatLevel.OK, "Matches a tested version.")
    try:
        live_version = SemVer.parse(live)
        tested_versions = [SemVer.parse(v) for v in policy.tested]
    except InvalidVersionError as exc:
        return verdict(CompatLevel.BLOCK_WRITES, f"Unparsable version: {exc}")

    if policy.exact or live_version.is_prerelease:
        return verdict(CompatLevel.BLOCK_WRITES, "This track requires an exact tested version.")

    same_major = [v for v in tested_versions if v.major == live_version.major]
    if not same_major:
        return verdict(CompatLevel.BLOCK_WRITES, "Different major version than tested.")
    if any(v.minor == live_version.minor for v in same_major):
        return verdict(CompatLevel.WARN, "Patch-level difference from the tested version.")
    if live_version.minor > max(v.minor for v in same_major):
        return verdict(CompatLevel.WARN, "Newer minor version than tested (backward compatible by SemVer).")
    return verdict(
        CompatLevel.BLOCK_WRITES,
        "Older minor version than tested; the gateway may lack features the agent relies on.",
    )


def assess_release(
    live_release: Mapping[str, str] | None,
    tested_release: Mapping[str, str],
) -> CompatReport:
    """Compare a deployment's release tracks with the tested release tracks.

    Args:
        live_release: Tracks reported by the deployment (``GET /version``), or ``None`` when
            they could not be retrieved. Unknown live state blocks all writes.
        tested_release: Tracks this agent build was tested with (from ``citadel.lock.json``).

    Returns:
        A report with one assessment per tested track, plus warnings for unknown live tracks.
    """
    live = dict(live_release or {})
    assessments: dict[str, TrackAssessment] = {}
    for track, policy in policies_from_release(tested_release).items():
        assessments[track] = assess_track(track, live.get(track), policy)
    for track, version in live.items():
        if track not in assessments:
            assessments[track] = TrackAssessment(
                track=track,
                live=version,
                tested=(),
                level=CompatLevel.WARN,
                reason="Track is not known to this agent build.",
            )
    return CompatReport(assessments=assessments)
