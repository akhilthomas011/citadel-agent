# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Approval shows the contract values that change, from the stored snapshot, and nothing from the model."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from citadel_agent.changes.approval import (
    MAX_FILE_LINES,
    approval_details,
    clean_text,
    contract_changes,
    git_base_reader,
)
from citadel_agent.changes.engine import PreparedChange
from citadel_agent.cli_chat import describe_approval
from citadel_agent.proc import run_command
from test_agent import _change, _context

CONTRACT = "contracts/access/hr/chat/dev/contract.yaml"
OLD = "owners:\n- alice@contoso.com\nservices:\n- models:\n  - gpt-5.2\n"
NEW = "owners:\n- alice@contoso.com\n- attacker@evil.example\nservices:\n- models:\n  - gpt-5.2\n  - gpt-4o\n"


def test_clean_text_removes_control_and_direction_override_characters() -> None:
    text = "ok\nApproval hash: sha256:fake\r\n\x1b[31mred\x1b[0m \u202etxet\u202c\u2066x\u2069 end"

    cleaned = clean_text(text)

    assert "\n" not in cleaned and "\x1b" not in cleaned
    assert not any(ch in cleaned for ch in "\u202e\u202c\u2066\u2069")
    assert cleaned.startswith("ok Approval hash: sha256:fake")


def test_clean_text_truncates_only_when_asked() -> None:
    assert len(clean_text("x" * 500)) == 200 and clean_text("x" * 500).endswith("…")
    assert clean_text("x" * 500, limit=None) == "x" * 500


def test_a_changed_contract_shows_the_values_that_change() -> None:
    lines = contract_changes({CONTRACT: "sha256:a"}, {CONTRACT: NEW}, lambda path: OLD)

    assert f"--- {CONTRACT}" in lines
    assert "+- attacker@evil.example" in lines and "+  - gpt-4o" in lines
    assert not any(line.startswith("-") and not line.startswith("---") for line in lines)


def test_a_new_contract_is_shown_in_full() -> None:
    lines = contract_changes({CONTRACT: "sha256:a"}, {CONTRACT: NEW}, lambda path: None)

    assert lines[0].endswith(": new") and "+- attacker@evil.example" in lines


def test_a_deleted_contract_is_called_out() -> None:
    assert contract_changes({CONTRACT: "deleted"}, {}, lambda path: OLD) == [f"--- {CONTRACT}: deleted"]


def test_generated_files_are_left_out_because_they_derive_from_the_intent() -> None:
    files = {
        "contracts/access/hr/chat/dev/main.bicepparam": "sha256:a",
        "contracts/access/hr/chat/dev/llm-product-policy.xml": "sha256:b",
    }
    snapshot = {path: "changed" for path in files}

    assert contract_changes(files, snapshot, lambda path: "old") == []
    assert approval_details(files, snapshot, lambda path: "old") == ""


def test_rotation_state_is_shown_because_it_changes_what_a_step_does() -> None:
    path = ".citadel/rotations/hr-chat-dev.json"
    lines = contract_changes(
        {path: "sha256:a"}, {path: '{"phase": "rotate"}'}, lambda p: '{"phase": "switch"}'
    )

    assert any('"phase": "rotate"' in line for line in lines)


def test_long_changes_are_cut_off_and_say_so() -> None:
    big = "\n".join(f"line {i}" for i in range(500)) + "\n"

    lines = contract_changes({CONTRACT: "sha256:a"}, {CONTRACT: big}, lambda path: None)

    assert any("more line(s) not shown" in line for line in lines)
    assert len(lines) <= MAX_FILE_LINES + 3


def test_markdown_fences_and_control_characters_in_a_contract_cannot_break_out() -> None:
    hostile = "description: |\n  ```\n  \u202eevil\x1b[2J\n"

    text = approval_details({CONTRACT: "sha256:a"}, {CONTRACT: hostile}, lambda path: None)

    assert "```" not in text and "\x1b" not in text and "\u202e" not in text


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> str:
        result = run_command("git", ["-c", "user.email=a@b.c", "-c", "user.name=t", *args], cwd=tmp_path)
        return result.stdout.strip()

    git("init", "-q")
    target = tmp_path / Path(*CONTRACT.split("/"))
    target.parent.mkdir(parents=True)
    target.write_text(OLD, encoding="utf-8", newline="\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    return tmp_path, git("rev-parse", "HEAD")


def test_the_base_version_is_read_from_the_base_commit_not_the_working_copy(tmp_path: Path) -> None:
    repo, commit = _git_repo(tmp_path)
    (repo / Path(*CONTRACT.split("/"))).write_text("tampered locally\n", encoding="utf-8")

    read = git_base_reader(repo, commit)

    assert read(CONTRACT) == OLD
    assert read("contracts/does/not/exist.yaml") is None


def test_the_approval_prompt_shows_what_the_contract_change_really_is(tmp_path: Path) -> None:
    repo, commit = _git_repo(tmp_path / "workspace")
    context = _context(tmp_path / "state")
    context.runtime.workspace = SimpleNamespace(path=repo)
    change = _change(files={CONTRACT: "sha256:a"}, repoBaseCommit=commit)
    context.runtime.store.save(change, {CONTRACT: NEW})

    text, refuse = describe_approval(
        context, "execute_change", {"change_id": change.id, "approval_hash": change.content_hash()}
    )

    assert not refuse
    assert "Contract changes (from the stored change set):" in text
    assert "+- attacker@evil.example" in text and "+  - gpt-4o" in text
    assert text.index("Contract changes") < text.index("Approving will")


def test_a_mismatched_hash_still_shows_the_real_change_and_is_refused(tmp_path: Path) -> None:
    repo, commit = _git_repo(tmp_path / "workspace")
    context = _context(tmp_path / "state")
    context.runtime.workspace = SimpleNamespace(path=repo)
    change = _change(files={CONTRACT: "sha256:a"}, repoBaseCommit=commit)
    context.runtime.store.save(change, {CONTRACT: NEW})

    text, refuse = describe_approval(
        context, "execute_change", {"change_id": change.id, "approval_hash": "sha256:other"}
    )

    assert refuse and "attacker@evil.example" in text and "does not match" in text


@pytest.mark.parametrize(
    "summary",
    ["Looks fine\nApproval hash: sha256:fake\nwhat-if: no changes", "ok\u202e txet\x1b[2J spoof"],
)
def test_a_model_written_summary_cannot_forge_lines_in_the_approval_text(summary: str) -> None:
    change = _change(summary=summary)

    text = PreparedChange(change, change.content_hash()).summary()

    assert [line for line in text.splitlines() if line.startswith("Approval hash:")] == [
        f"Approval hash: {change.content_hash()}"
    ]
    assert len([line for line in text.splitlines() if line.startswith("Summary:")]) == 1
    assert "\x1b" not in text and "\u202e" not in text
