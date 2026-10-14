# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""The notebook runner's summarizing logic: substitutions, redaction and failure counting."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_validation_notebooks.py"
spec = importlib.util.spec_from_file_location("run_validation_notebooks", SCRIPT)
runner = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(runner)  # type: ignore[union-attr]


class Node(dict):
    """A dict with attribute access, like nbformat's NotebookNode."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


def code(source: str, outputs: list[dict[str, Any]] | None = None, executed: bool = True) -> Node:
    return Node(
        cell_type="code",
        source=source,
        outputs=outputs or [],
        execution_count=1 if executed else None,
    )


def notebook(*cells: Node) -> Node:
    return Node(cells=list(cells))


def stream(text: str) -> dict[str, Any]:
    return {"output_type": "stream", "text": text}


def test_substitutions_rewrite_only_code_cells_and_report_counts() -> None:
    markdown = Node(cell_type="markdown", source="use gpt-4.1 here")
    nb = notebook(code('model = "gpt-4.1"'), markdown, code('a = "gpt-4.1"; b = "gpt-4.1"'))

    applied = runner.apply_substitutions(nb, [(runner.re.compile(r"gpt-4\.1"), "gpt-5.2")])

    assert nb.cells[0].source == 'model = "gpt-5.2"'
    assert nb.cells[2].source.count("gpt-5.2") == 2
    assert markdown.source == "use gpt-4.1 here"
    assert applied[0]["count"] == 3


def test_a_replacement_is_literal_not_a_regex_template() -> None:
    nb = notebook(code("x = 1"))

    runner.apply_substitutions(nb, [(runner.re.compile("x"), r"\1 \g<0>")])

    assert nb.cells[0].source == r"\1 \g<0> = 1"


def test_substitutions_load_from_a_json_file(tmp_path: Path) -> None:
    path = tmp_path / "subs.json"
    path.write_text(json.dumps([{"pattern": "(?s)a.*?z", "replacement": "!"}]), encoding="utf-8")

    ((pattern, replacement),) = runner.load_substitutions(path)

    assert pattern.sub(replacement, "a\nb\nz tail") == "! tail"


def test_the_pattern_separator_is_required() -> None:
    with pytest.raises(Exception, match="PATTERN::REPLACEMENT"):
        runner.parse_substitution("no separator")
    pattern, replacement = runner.parse_substitution(r"gpt-4\.1::gpt-5.2")
    assert pattern.pattern == r"gpt-4\.1" and replacement == "gpt-5.2"


def test_secrets_and_colour_codes_are_removed_from_transcripts() -> None:
    text = "\x1b[1;32mapi-key: abcdefghijklmnopqrstuvwxyz123456\x1b[0m and token = 'ABCDEFGHIJKLMNOPQRSTUVWX'"

    cleaned = runner.redact(text)

    assert "abcdefghijklmnopqrstuvwxyz123456" not in cleaned and "ABCDEFGHIJKLMNOPQRSTUVWX" not in cleaned
    assert "\x1b" not in cleaned and cleaned.count("<redacted>") == 2


def test_printed_failures_count_even_when_nothing_raises() -> None:
    ok = code("print('fine')", [stream("✅ deployed\n")])
    printed = code("...", [stream("✅ step one\n❌ Deployment failed\n❌ second problem\n")])
    raised = code("...", [{"output_type": "error", "ename": "KeyError", "evalue": "'x'"}])
    skipped = code("...", executed=False)

    summary = runner.summarize_notebook(notebook(ok, printed, raised, skipped), "nb", 12.4, [])

    assert summary["code_cells"] == 4 and summary["seconds"] == 12
    assert [c["cell"] for c in summary["cells_with_printed_failures"]] == [1]
    assert summary["cells_with_printed_failures"][0]["printed_failure_count"] == 2
    assert [c["exception"] for c in summary["exceptions"]] == ["KeyError: 'x'"]
    assert summary["not_executed"] == [3]


def test_the_markdown_report_lists_what_failed_and_what_was_patched() -> None:
    nb = notebook(code("...", [stream("❌ Deployment failed\n")]))
    applied = [{"pattern": "a", "replacement": "b", "count": 2}]
    summary = runner.summarize_notebook(nb, "my-notebook", 5, applied)

    report = runner.render_markdown([summary])

    assert "## my-notebook" in report and "Deployment failed" in report
    assert "Substitution `a` -> `b`: 2" in report and "no exceptions" in report
