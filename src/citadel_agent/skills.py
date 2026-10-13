# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Agent Skills: SKILL.md playbooks whose resources resolve to the pinned upstream at runtime.

Each playbook under ``skills/<name>/SKILL.md`` has Agent Skills frontmatter (``name``,
``description``) plus ``metadata.persona`` and ``metadata.docs``. The documentation citations
become skill resources that read the section from the pinned upstream when the model asks for
them, so playbooks never copy upstream text. Catalog-backed resources (error catalog, policy
variables, API surfaces, roles) are attached to the playbooks that need them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from citadel_agent.knowledge.catalogs import Catalogs
from citadel_agent.knowledge.docs import DocIndex

SKILL_FILE = "SKILL.md"
Persona = Literal["any", "consumer", "operator"]
_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_TOOL_REFERENCE = re.compile(r"`([a-z]+(?:_[a-z]+)+)`")
MAX_DESCRIPTION = 200
MAX_INSTRUCTIONS = 12000


class SkillError(ValueError):
    """Raised when a playbook is malformed."""


@dataclass(frozen=True)
class Playbook:
    name: str
    description: str
    persona: Persona
    docs: tuple[str, ...]
    instructions: str
    path: Path

    @property
    def referenced_tools(self) -> set[str]:
        """Tool names the playbook mentions (backticked snake_case identifiers)."""
        return set(_TOOL_REFERENCE.findall(self.instructions))


def parse_playbook(path: Path) -> Playbook:
    """Parse and validate one ``SKILL.md``."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.S)
    if match is None:
        raise SkillError(f"{path}: missing YAML frontmatter")
    front = yaml.safe_load(match.group(1)) or {}
    body = match.group(2).strip()
    name = str(front.get("name", ""))
    description = str(front.get("description", "")).strip()
    metadata = front.get("metadata") or {}
    persona = str(metadata.get("persona", "any"))
    if not _NAME.match(name) or len(name) > 64:
        raise SkillError(f"{path}: invalid skill name '{name}'")
    if name != path.parent.name:
        raise SkillError(f"{path}: name '{name}' must match the folder name '{path.parent.name}'")
    if not description or len(description) > MAX_DESCRIPTION:
        raise SkillError(f"{path}: description must be 1-{MAX_DESCRIPTION} characters")
    if persona not in ("any", "consumer", "operator"):
        raise SkillError(f"{path}: metadata.persona must be any, consumer or operator")
    if not body or len(body) > MAX_INSTRUCTIONS:
        raise SkillError(f"{path}: instructions must be 1-{MAX_INSTRUCTIONS} characters")
    docs = tuple(str(metadata.get("docs", "")).split())
    return Playbook(name, description, persona, docs, body, path)  # type: ignore[arg-type]


def load_playbooks(root: Path) -> list[Playbook]:
    """Every playbook under ``root`` (one folder per skill), sorted by name."""
    return [parse_playbook(path) for path in sorted(root.glob(f"*/{SKILL_FILE}"))]


def playbooks_for(playbooks: Iterable[Playbook], persona: str) -> list[Playbook]:
    """Playbooks a persona can use. Operators see everything; consumers see non-operator skills."""
    if persona == "operator":
        return list(playbooks)
    return [p for p in playbooks if p.persona in ("any", persona)]


def resource_name(citation: str) -> str:
    """A skill resource name for a documentation citation (``guide-anchor``, at most 64 characters)."""
    path, _, anchor = citation.partition("#")
    stem = Path(path).stem.lower()
    if stem == "readme":
        stem = Path(path).parent.name.lower() or "readme"
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", f"{stem}-{anchor}".lower())).strip("-")
    if len(slug) > 64:
        slug = slug[:65].rsplit("-", 1)[0]
    return slug.strip("-")


def _error_catalog(catalogs: Catalogs) -> str:
    lines = ["| Id | Status | Codes | Meaning | Owner |", "|---|---|---|---|---|"]
    for error in catalogs.errors:
        codes = ", ".join(error.codes) or "-"
        lines.append(f"| {error.id} | {error.status or '-'} | {codes} | {error.title} | {error.owner} |")
    return "\n".join(lines)


def _policy_variables(catalogs: Catalogs) -> str:
    lines = ["| Policy variable | Contract field | Default | Meaning |", "|---|---|---|---|"]
    for v in catalogs.variables:
        lines.append(f"| {v.name} | {v.intent_field} | {v.default} | {v.description} |")
    return "\n".join(lines)


def _api_surfaces(catalogs: Catalogs) -> str:
    parts = []
    for surface in catalogs.surfaces:
        operations = "\n".join(f"* {op.method} `{op.path}`: {op.purpose}" for op in surface.operations)
        unsupported = "".join(f"\n* Not supported: {item}" for item in surface.unsupported)
        parts.append(
            f"## {surface.name} (`{surface.base_path}`)\n\nAuthentication: {surface.auth}\n"
            f"Model location: {surface.model_location}\n\n{operations}{unsupported}"
        )
    return "\n\n".join(parts)


def _roles(catalogs: Catalogs) -> str:
    lines = ["| Capability | Roles | Scope |", "|---|---|---|"]
    for c in catalogs.capabilities:
        lines.append(f"| {c.id}: {c.description} | {', '.join(c.roles) or 'none'} | {c.scope} |")
    return "\n".join(lines)


CATALOG_RESOURCES: dict[str, list[tuple[str, str, Callable[[Catalogs], str]]]] = {
    "troubleshooting": [("error-catalog", "Citadel error codes and their meaning", _error_catalog)],
    "access-contracts": [
        ("policy-variables", "Policy settings and their contract fields", _policy_variables)
    ],
    "calling-models": [("api-surfaces", "Gateway API surfaces and operations", _api_surfaces)],
    "operations-version": [("roles", "Azure roles needed per capability", _roles)],
}


def build_skills(playbooks: Iterable[Playbook], docs: DocIndex, catalogs: Catalogs) -> list[Any]:
    """Agent Framework skills for the playbooks, with lazily resolved resources."""
    from agent_framework import InlineSkill, InlineSkillResource, SkillFrontmatter

    def doc_reader(citation: str) -> Callable[[], str]:
        def read() -> str:
            text = docs.read(citation)
            if text is None:
                return f"The section {citation} is not available in the pinned upstream documentation."
            url = docs.url(docs.find(citation)) if docs.find(citation) else None  # type: ignore[arg-type]
            return f"Source: {url or citation}\n\n{text}"

        return read

    def catalog_reader(render: Callable[[Catalogs], str]) -> Callable[[], str]:
        return lambda: render(catalogs)

    skills = []
    for playbook in playbooks:
        resources = []
        for citation in playbook.docs:
            section = docs.find(citation)
            description = f"Upstream documentation: {section.title if section else citation}"
            resources.append(
                InlineSkillResource(
                    name=resource_name(citation), description=description, function=doc_reader(citation)
                )
            )
        for name, description, render in CATALOG_RESOURCES.get(playbook.name, []):
            resources.append(
                InlineSkillResource(name=name, description=description, function=catalog_reader(render))
            )
        skills.append(
            InlineSkill(
                frontmatter=SkillFrontmatter(
                    name=playbook.name,
                    description=playbook.description,
                    metadata={"persona": playbook.persona},
                ),
                instructions=playbook.instructions,
                resources=resources,
            )
        )
    return skills


def build_skills_provider(skills: list[Any]) -> Any:
    """A skills provider for trusted, read-only playbooks (no scripts, no approval prompts)."""
    from agent_framework import SkillsProvider

    return SkillsProvider(
        skills,
        disable_load_skill_approval=True,
        disable_read_skill_resource_approval=True,
    )
