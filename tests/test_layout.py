# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the upstream layout against the real pinned submodule."""

from __future__ import annotations

from pathlib import Path

import pytest

from citadel_agent.upstream.layout import UpstreamLayout, locate_upstream

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def layout() -> UpstreamLayout:
    if not (REPO_ROOT / "upstream" / "ai-hub-gateway" / "release.json").is_file():
        pytest.skip("Upstream submodule is not checked out (run: git submodule update --init).")
    return locate_upstream(REPO_ROOT)


def test_all_required_upstream_files_exist(layout: UpstreamLayout) -> None:
    assert layout.missing_files() == []


def test_contract_templates_are_bicep_files(layout: UpstreamLayout) -> None:
    templates = layout.contract_templates()
    assert set(templates) == {"access", "backend", "publish", "alerts"}
    assert all(path.suffix == ".bicep" for path in templates.values())


def test_documentation_includes_core_guides(layout: UpstreamLayout) -> None:
    docs = {layout.relative(path) for path in layout.documentation_files()}
    assert "README.md" in docs
    assert "bicep/infra/citadel-access-contracts/README.md" in docs
    assert "guides/llm-access-guide.md" in docs


def test_missing_files_reported_for_empty_checkout(tmp_path: Path) -> None:
    empty = UpstreamLayout(tmp_path)
    assert empty.missing_files() == empty.required_files()
