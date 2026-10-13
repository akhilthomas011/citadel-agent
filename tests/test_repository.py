# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the canonical contracts repository reader."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from citadel_agent.changes.repository import ADMIN_POLICY_PATH, CanonicalRepository
from citadel_agent.contracts import parse_intent, save_intent
from citadel_agent.proc import run_command

ACCESS = {
    "kind": "access-contract",
    "environment": "dev",
    "useCase": {"businessUnit": "HR", "useCaseName": "ChatAgent", "environment": "DEV"},
    "services": [{"code": "LLM", "apis": ["universal-llm-api"], "keySecretName": "HR-KEY"}],
}
BACKEND = {
    "kind": "backend-contract",
    "environment": "dev",
    "backends": [
        {
            "backendId": "aif-0",
            "backendType": "ai-foundry",
            "endpoint": "https://aif-0.cognitiveservices.azure.com/",
            "supportedModels": [{"name": "gpt-5.2"}],
        }
    ],
}


def git(cwd: Path, *args: str) -> str:
    return run_command("git", args, cwd=cwd).stdout.strip()


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "agent@example.com")
    git(path, "config", "user.name", "Citadel Agent Test")


@pytest.fixture
def repo(tmp_path: Path) -> CanonicalRepository:
    root = tmp_path / "contracts-repo"
    init_repo(root)
    upstream = root / "upstream" / "ai-hub-gateway"
    init_repo(upstream)
    (upstream / "release.json").write_text('{"master-version": "1.0.2"}', encoding="utf-8")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "upstream")
    (root / ".citadel").mkdir()
    (root / Path(*ADMIN_POLICY_PATH.parts)).write_text("version: 1\n", encoding="utf-8")
    save_intent(parse_intent(ACCESS), root / "contracts/access/hr/chatagent/dev/contract.yaml")
    save_intent(parse_intent(BACKEND), root / "contracts/backend/dev/contract.yaml")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return CanonicalRepository(root)


def test_layout_validation(repo: CanonicalRepository, tmp_path: Path) -> None:
    assert repo.validate_layout() == []
    problems = CanonicalRepository(tmp_path / "missing").validate_layout()
    assert len(problems) == 3


def test_intents_are_discovered_and_filtered(repo: CanonicalRepository) -> None:
    all_paths = [record.path for record in repo.intents()]
    assert all_paths == [
        PurePosixPath("contracts/access/hr/chatagent/dev/contract.yaml"),
        PurePosixPath("contracts/backend/dev/contract.yaml"),
    ]
    backends = repo.intents("backend-contract")
    assert len(backends) == 1 and backends[0].intent.environment == "dev"
    assert repo.find(PurePosixPath("contracts/backend/dev")) is not None
    assert repo.find(PurePosixPath("contracts/backend/prod")) is None


def test_git_state(repo: CanonicalRepository) -> None:
    assert len(repo.head_commit()) == 40
    assert len(repo.upstream_commit()) == 40
    assert repo.current_branch() == "main"
    assert repo.is_clean()
    (repo.root / "contracts" / "note.txt").write_text("x", encoding="utf-8")
    assert not repo.is_clean()
    assert repo.changed_paths() == ["contracts/note.txt"]
