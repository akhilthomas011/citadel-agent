#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Align the pipes of every Markdown table in the given files (in place).

Usage:
    uv run python scripts/align_md_tables.py README.md docs/*.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

EXIT_SUCCESS = 0
EXIT_FAILURE = 1

_ROW = re.compile(r"^\s*\|.*\|\s*$")
_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")


def _cells(line: str) -> list[str]:
    inner = line.strip()[1:-1]
    cells, current, in_code = [], [], False
    for char in inner:
        if char == "`":
            in_code = not in_code
        if char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


def _format_table(lines: list[str]) -> list[str]:
    rows = [_cells(line) for line in lines]
    count = max(len(r) for r in rows)
    rows = [r + [""] * (count - len(r)) for r in rows]
    separator_index = 1 if len(rows) > 1 and all(_SEPARATOR_CELL.match(c or "---") for c in rows[1]) else None
    widths = [
        max(3, *(len(r[i]) for idx, r in enumerate(rows) if idx != separator_index)) for i in range(count)
    ]
    out = []
    for idx, row in enumerate(rows):
        if idx == separator_index:
            out.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
        else:
            out.append("| " + " | ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) + " |")
    return out


def align_text(text: str) -> str:
    """Return ``text`` with every Markdown table aligned (code fences are left untouched)."""
    lines = text.split("\n")
    result: list[str] = []
    block: list[str] = []
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and _ROW.match(line):
            block.append(line)
            continue
        if block:
            result.extend(_format_table(block))
            block = []
        result.append(line)
    if block:
        result.extend(_format_table(block))
    return "\n".join(result)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Align Markdown table pipes in place")
    parser.add_argument("files", nargs="+", type=Path)
    return parser


def main() -> int:
    args = create_parser().parse_args()
    try:
        for path in args.files:
            original = path.read_text(encoding="utf-8")
            aligned = align_text(original)
            if aligned != original:
                path.write_text(aligned, encoding="utf-8", newline="\n")
                print(f"aligned {path}")
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
