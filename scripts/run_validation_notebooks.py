# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Run upstream's validation notebooks against a Citadel hub and summarize the results.

Upstream recommends running four notebooks, in order, on every new hub: backend onboarding,
all-models tests, access contracts and agent frameworks. They deploy resources, so run them from a
scratch copy of the upstream tree (they write generated files next to the templates) that also holds
the hub's ``.azure`` azd environment, never from the pinned submodule.

Notebooks print most failures instead of raising them, so the summary counts both exceptions and
lines the notebooks mark as failed.

Example (PowerShell):

    python scripts/run_validation_notebooks.py --workdir $env:TEMP\\citadel-nb \\
        --results $env:TEMP\\nb-results --sub "gpt-4\\.1::gpt-5.2"

Run it with a Python environment that has the notebooks' requirements, ``nbclient`` and ``ipykernel``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

BASELINE = (
    "llm-backend-onboarding-runner",
    "citadel-universal-llm-api-all-models-tests",
    "citadel-access-contracts-tests",
    "citadel-agent-frameworks-tests",
)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
FAILURE_MARKERS = ("❌",)
SECRET = re.compile(
    r"(?i)(api[-_ ]?key|secret|token|password)(['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9+/_\-.]{16,})"
)


def parse_substitution(text: str) -> tuple[re.Pattern[str], str]:
    pattern, separator, replacement = text.partition("::")
    if not separator:
        raise argparse.ArgumentTypeError("use PATTERN::REPLACEMENT")
    return re.compile(pattern, re.DOTALL), replacement


def load_substitutions(path: Path) -> list[tuple[re.Pattern[str], str]]:
    """Substitutions from a JSON file: a list of ``{"pattern": ..., "replacement": ...}``."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    return [(re.compile(e["pattern"], re.DOTALL), e["replacement"]) for e in entries]


def apply_substitutions(nb: Any, substitutions: list[tuple[re.Pattern[str], str]]) -> list[dict[str, Any]]:
    """Rewrite code cells and report how many replacements each substitution made."""
    applied = [{"pattern": p.pattern, "replacement": r, "count": 0} for p, r in substitutions]
    for cell in nb.cells:
        if cell.cell_type != "code":
            continue
        for index, (pattern, replacement) in enumerate(substitutions):
            cell.source, count = pattern.subn(lambda _m, r=replacement: r, cell.source)
            applied[index]["count"] += count
    return applied


def redact(text: str) -> str:
    return SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", ANSI.sub("", text))


def cell_text(cell: Any) -> str:
    parts: list[str] = []
    for output in cell.get("outputs", []):
        if output.get("output_type") == "stream":
            parts.append(output.get("text", ""))
        elif output.get("output_type") in ("execute_result", "display_data"):
            parts.append(str(output.get("data", {}).get("text/plain", "")))
        elif output.get("output_type") == "error":
            parts.append(f"{output.get('ename')}: {output.get('evalue')}")
    return redact("".join(parts))


def summarize_notebook(nb: Any, name: str, seconds: float, applied: list[dict[str, Any]]) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for index, cell in enumerate(nb.cells):
        if cell.cell_type != "code" or "outputs" not in cell:
            continue
        text = cell_text(cell)
        raised = [o for o in cell.outputs if o.get("output_type") == "error"]
        printed = [line.strip() for line in text.splitlines() if any(m in line for m in FAILURE_MARKERS)]
        cells.append(
            {
                "cell": index,
                "executed": cell.get("execution_count") is not None,
                "exception": f"{raised[0].get('ename')}: {raised[0].get('evalue')}"[:300] if raised else None,
                "printed_failures": printed[:6],
                "printed_failure_count": len(printed),
            }
        )
    return {
        "notebook": name,
        "seconds": round(seconds),
        "code_cells": len(cells),
        "not_executed": [c["cell"] for c in cells if not c["executed"]],
        "exceptions": [c for c in cells if c["exception"]],
        "cells_with_printed_failures": [c for c in cells if c["printed_failure_count"]],
        "substitutions": applied,
    }


async def run_one(
    name: str, workdir: Path, results: Path, timeout: int, substitutions: list[tuple[re.Pattern[str], str]]
) -> dict[str, Any]:
    import nbformat
    from nbclient import NotebookClient

    path = workdir / "validation" / f"{name}.ipynb"
    nb = nbformat.read(path, as_version=4)
    applied = apply_substitutions(nb, substitutions)
    progress = results / f"{name}.progress.log"

    async def on_cell(cell: Any, cell_index: int, execute_reply: Any) -> None:
        status = (execute_reply or {}).get("content", {}).get("status")
        with progress.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} cell {cell_index}: {status}\n")

    client = NotebookClient(
        nb,
        timeout=timeout,
        kernel_name="python3",
        allow_errors=True,
        resources={"metadata": {"path": str(path.parent)}},
        on_cell_executed=on_cell,
    )
    started = time.time()
    await client.async_execute()
    nbformat.write(nb, results / f"{name}.executed.ipynb")
    (results / f"{name}.transcript.txt").write_text(
        "\n".join(f"##### cell {i}\n{cell_text(c)}" for i, c in enumerate(nb.cells) if c.cell_type == "code"),
        encoding="utf-8",
    )
    return summarize_notebook(nb, name, time.time() - started, applied)


def render_markdown(summaries: list[dict[str, Any]]) -> str:
    lines = ["# Upstream validation notebooks", ""]
    for s in summaries:
        verdict = "no exceptions" if not s["exceptions"] else f"{len(s['exceptions'])} cell(s) raised"
        lines += [
            f"## {s['notebook']}",
            f"* {s['code_cells']} code cells in {s['seconds']} s: {verdict}, "
            f"{len(s['cells_with_printed_failures'])} cell(s) printed failures, "
            f"{len(s['not_executed'])} not executed",
        ]
        lines += [
            f"* Substitution `{a['pattern']}` -> `{a['replacement']}`: {a['count']}"
            for a in s["substitutions"]
        ]
        for c in s["exceptions"]:
            lines.append(f"* Cell {c['cell']} raised: {c['exception']}")
        for c in s["cells_with_printed_failures"]:
            lines.append(f"* Cell {c['cell']} printed {c['printed_failure_count']} failure line(s):")
            lines += [f"  * {text[:240]}" for text in c["printed_failures"]]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--workdir", type=Path, required=True, help="Scratch copy of the upstream tree.")
    parser.add_argument(
        "--results", type=Path, required=True, help="Folder for executed notebooks and reports."
    )
    parser.add_argument(
        "--notebook", action="append", help="Notebook name (default: the four baseline ones)."
    )
    parser.add_argument("--cell-timeout", type=int, default=1200, help="Seconds allowed per cell.")
    parser.add_argument(
        "--sub",
        action="append",
        default=[],
        type=parse_substitution,
        help="PATTERN::REPLACEMENT for code cells.",
    )
    parser.add_argument(
        "--subs-file", type=Path, help="JSON list of {pattern, replacement} applied to code cells."
    )
    args = parser.parse_args(argv)
    if args.subs_file:
        args.sub = [*args.sub, *load_substitutions(args.subs_file)]

    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    args.results.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    summaries = []
    for name in args.notebook or BASELINE:
        print(f"Running {name} ...", flush=True)
        summary = asyncio.run(run_one(name, args.workdir, args.results, args.cell_timeout, args.sub))
        summaries.append(summary)
        (args.results / f"{name}.summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print(
            f"  done in {summary['seconds']} s: {len(summary['exceptions'])} raised, "
            f"{len(summary['cells_with_printed_failures'])} with printed failures",
            flush=True,
        )
    report = render_markdown(summaries)
    (args.results / "report.md").write_text(report, encoding="utf-8")
    return 1 if any(s["exceptions"] for s in summaries) else 0


if __name__ == "__main__":
    raise SystemExit(main())
