# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the Agent Skills playbooks and their runtime resources."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from citadel_agent.knowledge import DocIndex, build_doc_index, load_catalogs
from citadel_agent.knowledge.docs import parse_markdown
from citadel_agent.skills import (
    SkillError,
    build_skills,
    build_skills_provider,
    load_playbooks,
    parse_playbook,
    playbooks_for,
    resource_name,
)
from citadel_agent.tools import TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
UPSTREAM = ROOT / "upstream" / "ai-hub-gateway"
EXPECTED = {
    "access-contracts",
    "alerts",
    "backend-onboarding",
    "calling-models",
    "citadel-overview",
    "foundry-connection",
    "key-rotation",
    "operations-version",
    "publish-contracts",
    "troubleshooting",
    "usage-and-cost",
}


def test_all_playbooks_parse() -> None:
    playbooks = load_playbooks(SKILLS)
    assert {p.name for p in playbooks} == EXPECTED
    assert all(p.docs for p in playbooks)


def test_playbooks_only_mention_known_tools() -> None:
    for playbook in load_playbooks(SKILLS):
        unknown = playbook.referenced_tools - TOOL_NAMES
        assert not unknown, f"{playbook.name} mentions unknown tools {sorted(unknown)}"


@pytest.mark.skipif(not (UPSTREAM / "release.json").is_file(), reason="Upstream submodule is not checked out")
def test_playbook_citations_exist_in_the_pinned_upstream() -> None:
    index = build_doc_index(UPSTREAM)
    missing = [(p.name, c) for p in load_playbooks(SKILLS) for c in p.docs if index.find(c) is None]
    assert missing == []


def test_persona_filtering() -> None:
    playbooks = load_playbooks(SKILLS)
    consumer = {p.name for p in playbooks_for(playbooks, "consumer")}
    assert {"backend-onboarding", "publish-contracts", "operations-version"}.isdisjoint(consumer)
    assert "troubleshooting" in consumer
    assert {p.name for p in playbooks_for(playbooks, "operator")} == EXPECTED


@pytest.mark.parametrize(
    ("citation", "name"),
    [
        (
            "bicep/infra/llm-backend-onboarding/README.md#model-aliases",
            "llm-backend-onboarding-model-aliases",
        ),
        ("guides/llm-access-guide.md#retry-logic", "llm-access-guide-retry-logic"),
        ("README.md#-overview", "readme-overview"),
        (
            "bicep/infra/citadel-access-contracts/access-contract-key-rotation-guide.md"
            "#-the-3-step-zero-downtime-rotation",
            "access-contract-key-rotation-guide-the-3-step-zero-downtime",
        ),
    ],
)
def test_resource_names(citation: str, name: str) -> None:
    assert resource_name(citation) == name


def test_skills_resolve_resources_lazily(tmp_path: Path) -> None:
    folder = tmp_path / "troubleshooting"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: troubleshooting\ndescription: Diagnose failed calls.\n"
        'metadata:\n  persona: any\n  docs: "guides/errors.md#retry"\n---\n\n'
        "# Troubleshooting\n\nUse `explain_error`.\n",
        encoding="utf-8",
    )
    index = DocIndex(
        parse_markdown("guides/errors.md", "# Errors\n\n## Retry\n\nRetry twice.\n"),
        base_url="https://github.com/o/r/blob/abc",
    )
    skill = build_skills(load_playbooks(tmp_path), index, load_catalogs())[0]
    content = asyncio.run(skill.get_content())
    assert 'resource name="errors-retry"' in content and 'resource name="error-catalog"' in content

    doc = asyncio.run(asyncio.run(skill.get_resource("errors-retry")).read())
    assert doc.startswith("Source: https://github.com/o/r/blob/abc/guides/errors.md#retry")
    assert "Retry twice." in doc
    catalog = asyncio.run(asyncio.run(skill.get_resource("error-catalog")).read())
    assert "unauthorized_model_access" in catalog
    assert build_skills_provider([skill]) is not None


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("# No frontmatter\n", "frontmatter"),
        ("---\nname: other\ndescription: d\n---\n\nBody\n", "must match the folder"),
        ("---\nname: sample\ndescription: d\nmetadata:\n  persona: admin\n---\n\nBody\n", "persona"),
        ("---\nname: sample\ndescription: ''\n---\n\nBody\n", "description"),
    ],
)
def test_malformed_playbooks_are_rejected(tmp_path: Path, content: str, message: str) -> None:
    folder = tmp_path / "sample"
    folder.mkdir()
    (folder / "SKILL.md").write_text(content, encoding="utf-8")
    with pytest.raises(SkillError, match=message):
        parse_playbook(folder / "SKILL.md")
