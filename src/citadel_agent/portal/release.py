# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Strict, read-only release evidence checks; unavailable protection metadata never means approved."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Literal

from citadel_agent.contracts.common import CitadelModel
from citadel_agent.proc import CommandError, run_command


class ReleaseGate(CitadelModel):
    name: str
    status: Literal["pass", "blocked", "unknown"]
    detail: str


class ReleaseReadiness(CitadelModel):
    release_ready: bool
    source_sha: str
    checks: list[ReleaseGate]
    live_environment_verified: bool = False


def branch_gate(metadata: dict[str, Any] | None, required_checks: tuple[str, ...]) -> ReleaseGate:
    if metadata is None:
        return ReleaseGate(
            name="branch-protection", status="unknown", detail="Branch protection could not be retrieved."
        )
    reviews = metadata.get("required_pull_request_reviews") or {}
    configured = set((metadata.get("required_status_checks") or {}).get("contexts") or [])
    configured.update(
        item.get("context") for item in (metadata.get("required_status_checks") or {}).get("checks", [])
    )
    missing = (
        reviews.get("required_approving_review_count", 0) < 1
        or reviews.get("dismiss_stale_reviews") is not True
        or (metadata.get("enforce_admins") or {}).get("enabled") is not True
        or (metadata.get("allow_force_pushes") or {}).get("enabled") is True
        or (metadata.get("allow_deletions") or {}).get("enabled") is True
        or not required_checks
        or not set(required_checks) <= configured
    )
    return ReleaseGate(
        name="branch-protection",
        status="blocked" if missing else "pass",
        detail="Require PR approval, stale-review dismissal, enforced admins, "
        "no destructive pushes and every selected check."
        if missing
        else "Required branch review and exact selected checks are enforced.",
    )


def environment_gate(metadata: dict[str, Any] | None) -> ReleaseGate:
    if metadata is None:
        return ReleaseGate(
            name="production-environment",
            status="unknown",
            detail="Environment protection metadata could not be retrieved.",
        )
    rules = metadata.get("protection_rules") or []
    review = next((item for item in rules if item.get("type") == "required_reviewers"), {})
    branches = metadata.get("deployment_branch_policy") or {}
    passed = (
        bool(review.get("reviewers"))
        and review.get("prevent_self_review") is True
        and metadata.get("can_admins_bypass") is False
        and branches.get("protected_branches") is True
    )
    return ReleaseGate(
        name="production-environment",
        status="pass" if passed else "blocked",
        detail="Require independent reviewers, self-review prevention, no admin bypass "
        "and protected deployment branches."
        if not passed
        else "Independent production environment approval and protected branches are enforced.",
    )


def _github_json(path: str) -> dict[str, Any] | None:
    try:
        result = run_command("gh", ["api", path], check=False, timeout=30)
    except (CommandError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def evaluate_release(
    root: Path,
    *,
    manifest: Path | None,
    repository: str,
    contracts_repository: str | None,
    required_checks: tuple[str, ...],
    branch: str = "main",
    environment: str = "production",
    remote: bool = False,
) -> ReleaseReadiness:
    if any(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None
        for value in (repository, *([contracts_repository] if contracts_repository else []))
    ):
        raise ValueError("repositories must use owner/name form")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", branch) or not re.fullmatch(r"[A-Za-z0-9._-]+", environment):
        raise ValueError("branch and environment identifiers are invalid")
    source = run_command("git", ["-C", str(root), "rev-parse", "HEAD"], timeout=10).stdout.strip()
    dirty = run_command("git", ["-C", str(root), "status", "--porcelain"], timeout=10).stdout.strip()
    checks = [
        ReleaseGate(
            name="reviewable-source",
            status="blocked" if dirty else "pass",
            detail="Uncommitted source is not an immutable reviewed release."
            if dirty
            else "The checkout is clean.",
        )
    ]
    if manifest is None or not manifest.is_file():
        checks.append(
            ReleaseGate(
                name="published-artifact",
                status="blocked",
                detail="A verified publication manifest is required; local build output is not a release.",
            )
        )
    else:
        value = json.loads(manifest.read_text(encoding="utf-8"))
        name = value.get("file")
        artifact = manifest.parent / name if isinstance(name, str) and Path(name).name == name else None
        passed = (
            artifact is not None
            and artifact.is_file()
            and hashlib.sha256(artifact.read_bytes()).hexdigest() == value.get("sha256")
            and value.get("sourceSha") == source
            and value.get("imagePublished") is True
            and re.fullmatch(r"ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", value.get("agentImage", ""))
            is not None
        )
        checks.append(
            ReleaseGate(
                name="published-artifact",
                status="pass" if passed else "blocked",
                detail="Manifest must bind the ZIP hash, source commit and published immutable image digest.",
            )
        )
    if remote:
        for label, repo in (("agent", repository), ("contracts", contracts_repository)):
            if repo is None:
                checks.append(
                    ReleaseGate(
                        name=label + "-repository",
                        status="unknown",
                        detail="The canonical contracts repository was not supplied.",
                    )
                )
                continue
            protection = branch_gate(
                _github_json(f"repos/{repo}/branches/{branch}/protection"), required_checks
            )
            protection.name = label + "-" + protection.name
            checks.append(protection)
            target = environment_gate(_github_json(f"repos/{repo}/environments/{environment}"))
            target.name = label + "-" + target.name
            checks.append(target)
    else:
        checks.append(
            ReleaseGate(
                name="remote-governance",
                status="unknown",
                detail="Run read-only remote checks; local templates do not prove repository protections.",
            )
        )
    return ReleaseReadiness(
        release_ready=all(item.status == "pass" for item in checks), source_sha=source, checks=checks
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--contracts-repository")
    parser.add_argument("--required-check", action="append", default=[])
    parser.add_argument("--branch", default="main")
    parser.add_argument("--environment", default="production")
    parser.add_argument(
        "--remote", action="store_true", help="Read GitHub protection metadata; never modify it."
    )
    args = parser.parse_args(argv)
    result = evaluate_release(
        args.root,
        manifest=args.manifest,
        repository=args.repository,
        contracts_repository=args.contracts_repository,
        required_checks=tuple(args.required_check),
        branch=args.branch,
        environment=args.environment,
        remote=args.remote,
    )
    print(result.model_dump_json(by_alias=True, indent=2))
    return 0 if result.release_ready else 1


if __name__ == "__main__":
    sys.exit(main())
