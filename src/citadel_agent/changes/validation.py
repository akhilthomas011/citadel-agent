# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Validate a canonical contracts repository (used by CI and by the agent before proposing changes)."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from citadel_agent.azure_clients.bicep import BicepCompileError, CompiledDeployment, compile_deployment
from citadel_agent.changes.repository import CITADEL_DIR, CONTRACTS_DIR, ROTATIONS_DIR, CanonicalRepository
from citadel_agent.contracts.access import AccessContractIntent
from citadel_agent.contracts.intent_io import load_intent
from citadel_agent.contracts.policy_xml import LintLevel, lint_policy
from citadel_agent.contracts.render import RenderContext, RenderError, render_intent
from citadel_agent.policy.admin import LoadedPolicy
from citadel_agent.policy.authz import cap_violations
from citadel_agent.safety.redaction import Redactor

_USING = re.compile(r"^\s*using\s+'(?P<path>[^']+)'", re.MULTILINE)
_TEXT_SUFFIXES = {".yaml", ".yml", ".json", ".xml", ".bicepparam", ".md", ".txt"}


class IssueLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class ValidationIssue:
    level: IssueLevel
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.level.value}: {self.path}: {self.message}"


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _active_rotations(repo: CanonicalRepository) -> set[str]:
    """Contract directories with a rotation transaction in its 'rotate' phase."""
    active: set[str] = set()
    folder = repo.path(ROTATIONS_DIR)
    if folder.is_dir():
        for file in folder.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if data.get("phase") == "rotate" and data.get("contractDir"):
                active.add(str(data["contractDir"]))
    return active


def validate_repository(
    repo: CanonicalRepository,
    loaded: LoadedPolicy,
    *,
    compile_contracts: bool = False,
    compile_fn: Callable[[Path], CompiledDeployment] = compile_deployment,
) -> list[ValidationIssue]:
    """Return every problem found in the repository (empty when valid)."""
    issues: list[ValidationIssue] = []

    def error(path: str, message: str) -> None:
        issues.append(ValidationIssue(IssueLevel.ERROR, path, message))

    if loaded.policy is None:
        error(".citadel/admin-policy.yaml", loaded.error or "admin policy unavailable")
        return issues
    policy = loaded.policy
    upstream_prefix = str(repo.upstream_path) + "/"
    contracts_root = repo.path(CONTRACTS_DIR)

    # Files that must never appear under contracts/.
    for path in sorted(contracts_root.rglob("*.bicep")) if contracts_root.is_dir() else []:
        error(
            path.relative_to(repo.root).as_posix(), "templates are not allowed in contracts/ (use upstream/)"
        )

    # Secret scan of every text file in contracts/ and .citadel/.
    scanner = Redactor()
    for base in (contracts_root, repo.path(CITADEL_DIR)):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in _TEXT_SUFFIXES:
                text = path.read_text(encoding="utf-8", errors="replace")
                if scanner.redact(text) != text:
                    error(path.relative_to(repo.root).as_posix(), "contains a secret-shaped value")

    rotating = _active_rotations(repo)
    secret_owners: dict[tuple[str, str], str] = {}
    upstream_commit = ""
    try:
        upstream_commit = repo.upstream_commit()
    except Exception:
        error(str(repo.upstream_path), "upstream submodule is not checked out")
        return issues

    for relative in repo.iter_intent_files():
        rel = str(relative)
        try:
            intent = load_intent(repo.path(relative))
        except (ValidationError, ValueError) as exc:
            error(rel, f"invalid intent: {exc}")
            continue
        if intent.environment not in policy.environments:
            error(rel, f"environment '{intent.environment}' is not defined in the admin policy")
            continue
        env = policy.environment(intent.environment)
        for problem in cap_violations(intent, env.caps):
            error(rel, problem)
        if isinstance(intent, AccessContractIntent) and env.key_vault is not None:
            vault = env.key_vault.name.lower()
            for secret in sorted(intent.key_vault_secret_names()):
                owner = secret_owners.setdefault((vault, secret), rel)
                if owner != rel:
                    error(rel, f"Key Vault secret '{secret}' in '{vault}' is also written by {owner}")
        if (
            isinstance(intent, AccessContractIntent)
            and intent.key_rotation.key_rotation_enabled
            and str(relative.parent) not in rotating
        ):
            error(rel, "keyRotationEnabled may only be true during an active rotation transaction")

        try:
            rendered = render_intent(
                intent,
                RenderContext(
                    repo_root=repo.root,
                    upstream_root=repo.upstream_root,
                    target=env.to_target(intent.environment),
                    upstream_commit=upstream_commit,
                ),
            )
        except (RenderError, ValueError) as exc:
            error(rel, f"cannot render: {exc}")
            continue
        for path, content in rendered.files.items():
            if path.name == "contract.yaml":
                continue
            on_disk = repo.path(path)
            if not on_disk.is_file():
                error(
                    str(path), "generated file is missing; regenerate with 'citadel-agent contracts render'"
                )
            elif on_disk.read_text(encoding="utf-8") != content:
                error(str(path), "generated file is out of date or was edited by hand; regenerate it")

        params_file = repo.path(rendered.deployment.parameters_file)
        if params_file.is_file():
            match = _USING.search(params_file.read_text(encoding="utf-8"))
            target = (
                posixpath.normpath(posixpath.join(str(rendered.directory), match["path"])) if match else ""
            )
            if not target.startswith(upstream_prefix):
                error(
                    str(rendered.deployment.parameters_file),
                    "the 'using' path must point into the upstream submodule",
                )
        for path in repo.path(rendered.directory).glob("*.xml"):
            for issue in lint_policy(path.read_text(encoding="utf-8")):
                level = IssueLevel.ERROR if issue.level is LintLevel.ERROR else IssueLevel.WARNING
                issues.append(ValidationIssue(level, path.relative_to(repo.root).as_posix(), issue.message))

        if compile_contracts and params_file.is_file():
            try:
                compile_fn(params_file)
            except BicepCompileError as exc:
                error(str(rendered.deployment.parameters_file), f"does not compile: {exc}")

    # Change-set manifests must match the files they describe.
    changesets = repo.path(PurePosixPath(CITADEL_DIR) / "changesets")
    for manifest_path in sorted(changesets.glob("*.json")) if changesets.is_dir() else []:
        rel = manifest_path.relative_to(repo.root).as_posix()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            error(rel, f"invalid JSON: {exc}")
            continue
        if manifest.get("executedBy") == "direct":
            continue
        for path, digest in (manifest.get("files") or {}).items():
            current = repo.path(path)
            if digest == "deleted":
                if current.exists():
                    error(rel, f"{path} should have been deleted")
            elif not current.is_file() or _sha256(current.read_text(encoding="utf-8")) != digest:
                error(rel, f"{path} differs from the prepared change set; prepare the change again")
    return issues
