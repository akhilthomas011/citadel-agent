# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""What a person sees before approving a change: the contract values that actually change.

The what-if preview lists resources and property paths but deliberately never values, so on its
own it cannot show that a change adds an owner or widens the allowed models. This module renders the
difference of the stored contract intents against the repository base commit. It reads only the
immutable snapshot taken at prepare time and Git, never the language model's description.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from citadel_agent.proc import CommandError, run_command

MAX_FILE_LINES = 60
MAX_TOTAL_LINES = 140
MAX_LINE_LENGTH = 200
# Control characters, and the Unicode bidirectional overrides that can reorder or hide text.
_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_INTENT_FILE = re.compile(r"(^|/)contract\.yaml$|^\.citadel/rotations/[^/]+\.json$")

BaseReader = Callable[[str], "str | None"]


def clean_text(text: str, limit: int | None = MAX_LINE_LENGTH) -> str:
    """One printable line: control and direction-override characters removed, whitespace collapsed."""
    collapsed = " ".join(_UNSAFE.sub(" ", str(text)).split())
    if limit is None or len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _clean_line(line: str) -> str:
    """One line of a file: unsafe characters removed, indentation kept (it carries YAML nesting)."""
    cleaned = _UNSAFE.sub(" ", line).replace("\t", "  ").rstrip().replace("```", "'''")
    return cleaned if len(cleaned) <= MAX_LINE_LENGTH else cleaned[: MAX_LINE_LENGTH - 1] + "…"


def _clean_lines(text: str) -> list[str]:
    return [_clean_line(line) for line in text.splitlines()]


def git_base_reader(workspace: Path, base_commit: str) -> BaseReader:
    """Read a file as it was at the base commit of the change (None when it did not exist)."""

    def read(path: str) -> str | None:
        try:
            return run_command("git", ["show", f"{base_commit}:{path}"], cwd=workspace).stdout
        except (CommandError, OSError):
            return None

    return read


def contract_changes(
    files: Mapping[str, str], snapshot: Mapping[str, str], read_base: BaseReader
) -> list[str]:
    """Unified diffs of every contract intent and agent state file the change touches.

    ``files`` maps repository paths to their digest (``"deleted"`` for removals) and ``snapshot``
    holds the stored contents. Generated files are left out: they derive from the intents.
    """
    lines: list[str] = []
    for path in sorted(p for p in files if _INTENT_FILE.search(p)):
        old = read_base(path)
        if files[path] == "deleted":
            lines.append(f"--- {clean_text(path)}: deleted")
            continue
        new = snapshot.get(path)
        if new is None:
            lines.append(f"--- {clean_text(path)}: not in the stored change set")
            continue
        old_lines, new_lines = _clean_lines(old or ""), _clean_lines(new)
        if old is None:
            body = [f"+{line}" for line in new_lines]
            header = [f"--- {clean_text(path)}: new"]
        else:
            body = list(difflib.unified_diff(old_lines, new_lines, "base", "change", lineterm="", n=1))[2:]
            header = [f"--- {clean_text(path)}"]
        if not body:
            continue
        shown = body[:MAX_FILE_LINES]
        lines += header + shown
        if len(body) > len(shown):
            lines.append(f"... {len(body) - len(shown)} more line(s) not shown")
        if len(lines) > MAX_TOTAL_LINES:
            lines = [*lines[:MAX_TOTAL_LINES], "... further contract changes not shown"]
            break
    return lines


def approval_details(files: Mapping[str, str], snapshot: Mapping[str, str], read_base: BaseReader) -> str:
    """The contract-change section of the approval text, or an empty string when there is none."""
    changes = contract_changes(files, snapshot, read_base)
    if not changes:
        return ""
    return "Contract changes (from the stored change set):\n" + "\n".join(f"  {line}" for line in changes)
