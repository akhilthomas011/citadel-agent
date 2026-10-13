# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Versioned knowledge catalogs: errors, API surfaces, policy variables, RBAC, naming, what-if noise.

The catalogs (YAML under ``knowledge/data``) are agent-owned data with citations into the pinned
upstream. Evidence strings tie entries to upstream source files, and :func:`catalog_drift` reports
entries whose evidence or documentation anchors no longer exist upstream (run by the drift tests
and ``upstream sync``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from citadel_agent.knowledge.docs import DocIndex

Verified = Literal["live", "source", "docs"]


class CatalogModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True)


class Evidence(CatalogModel):
    file: str
    contains: str


class ErrorEntry(CatalogModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str
    status: int | None = None
    codes: list[str] = Field(default_factory=list)
    message_pattern: str | None = None
    log_message_pattern: str | None = None
    requires_header: str | None = None
    surfaces: list[Literal["llm", "mcp", "a2a", "azure", "entra"]] = Field(default_factory=lambda: ["llm"])
    source: Literal["citadel-policy", "agent-policy", "apim-platform", "azure", "entra"]
    owner: Literal["consumer", "operator", "identity-admin"]
    causes: list[str] = Field(min_length=1)
    fixes: list[str] = Field(min_length=1)
    checks: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    docs: list[str] = Field(default_factory=list)
    verified: Verified

    @field_validator("message_pattern", "log_message_pattern")
    @classmethod
    def _valid_regex(cls, value: str | None) -> str | None:
        if value is not None:
            re.compile(value)
        return value


class Operation(CatalogModel):
    method: str
    path: str
    purpose: str


class ApiSurface(CatalogModel):
    id: str
    name: str
    apim_api_id: str
    base_path: str
    recommended: bool
    model_location: str
    auth: str
    operations: list[Operation]
    unsupported: list[str] = Field(default_factory=list)
    sdk_base_url: str | None = None
    notes: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    docs: list[str] = Field(default_factory=list)
    verified: Verified


class PolicyVariable(CatalogModel):
    name: str
    intent_field: str
    type: str
    default: str
    description: str
    evidence: list[Evidence] = Field(default_factory=list)


class WhatIfNoise(CatalogModel):
    id: str
    resource_pattern: str
    change_types: list[str]
    property_paths: list[str] | None = None
    explanation: str

    def matches(self, resource_id: str, change_type: str, property_changes: Iterable[str]) -> bool:
        if change_type not in self.change_types or not re.search(self.resource_pattern, resource_id, re.I):
            return False
        if self.property_paths is None:
            return True
        paths = {change.split(" (", 1)[0] for change in property_changes}
        return bool(paths) and paths <= set(self.property_paths)


class RbacCapability(CatalogModel):
    id: str
    description: str
    roles: list[str]
    scope: str
    notes: str | None = None
    docs: list[str] = Field(default_factory=list)


class NamingRule(CatalogModel):
    id: str
    pattern: str
    example: str
    notes: str | None = None


class Catalogs(CatalogModel):
    errors: list[ErrorEntry]
    surfaces: list[ApiSurface]
    variables: list[PolicyVariable]
    noise: list[WhatIfNoise]
    capabilities: list[RbacCapability]
    names: list[NamingRule]

    def error(self, error_id: str) -> ErrorEntry | None:
        return next((e for e in self.errors if e.id == error_id), None)

    def surface(self, surface_id: str) -> ApiSurface | None:
        return next((s for s in self.surfaces if s.id == surface_id), None)

    def variable(self, name: str) -> PolicyVariable | None:
        lowered = name.lower()
        return next((v for v in self.variables if v.name.lower() == lowered), None)

    def capability(self, capability_id: str) -> RbacCapability | None:
        return next((c for c in self.capabilities if c.id == capability_id), None)

    def match_errors(
        self,
        *,
        status: int | None = None,
        body: str | Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        log_message: str | None = None,
        surface: str = "llm",
    ) -> list[ErrorEntry]:
        """Catalog entries that explain a failed call, best match first.

        A matching error code wins, then a matching message (or gateway log message), then an
        entry that matches on the HTTP status alone.
        """
        text, code = _error_text_and_code(body)
        header_names = {name.lower() for name in (headers or {})}
        ranked: list[tuple[int, int, ErrorEntry]] = []
        for order, entry in enumerate(self.errors):
            if surface not in entry.surfaces:
                continue
            if entry.status is not None and status is not None and entry.status != status:
                continue
            if entry.codes:
                if code is None or code not in entry.codes:
                    continue
                score = 3
            elif (
                entry.log_message_pattern
                and log_message
                and re.search(entry.log_message_pattern, log_message, re.I)
            ):
                score = 3
            elif entry.requires_header and entry.requires_header.lower() in header_names:
                score = 2
            elif entry.message_pattern:
                if not text or not re.search(entry.message_pattern, text, re.I):
                    continue
                score = 2
            elif status is None or entry.requires_header:
                continue
            else:
                score = 1
            ranked.append((-score, order, entry))
        return [entry for _, _, entry in sorted(ranked, key=lambda item: (item[0], item[1]))]

    def classify_noise(
        self,
        resource_id: str,
        change_type: str,
        property_changes: Iterable[str] = (),
        created: Iterable[str] = (),
    ) -> WhatIfNoise | None:
        """The known-noise entry for a predicted change, if any.

        ``created`` holds the IDs of resources created by the same deployment: changes to their
        children are real (for example the API links of a new product), never noise.
        """
        lowered = resource_id.lower()
        if any(lowered.startswith(parent.lower() + "/") for parent in created):
            return None
        changes = list(property_changes)
        return next((n for n in self.noise if n.matches(resource_id, change_type, changes)), None)


def _error_text_and_code(body: str | Mapping[str, Any] | None) -> tuple[str, str | None]:
    if body is None:
        return "", None
    data: Any = body
    if isinstance(body, str):
        try:
            data = json.loads(body)
        except ValueError:
            return body, None
    if not isinstance(data, Mapping):
        return str(data), None
    error = data.get("error") if isinstance(data.get("error"), Mapping) else data
    code = error.get("code") if isinstance(error, Mapping) else None
    message = error.get("message") if isinstance(error, Mapping) else None
    text = json.dumps(data) if message is None else f"{message} {json.dumps(data)}"
    return text, str(code) if code else None


def _load(name: str) -> Any:
    content = resources.files("citadel_agent.knowledge").joinpath("data", name).read_text(encoding="utf-8")
    return yaml.safe_load(content)


@lru_cache(maxsize=1)
def load_catalogs() -> Catalogs:
    """Load and validate the packaged catalogs."""
    return Catalogs(
        errors=_load("errors.yaml")["errors"],
        surfaces=_load("api-surfaces.yaml")["surfaces"],
        variables=_load("policy-variables.yaml")["variables"],
        noise=_load("whatif-noise.yaml")["noise"],
        capabilities=_load("rbac.yaml")["capabilities"],
        names=_load("naming.yaml")["names"],
    )


def _entries_with_references(catalogs: Catalogs) -> Iterable[tuple[str, list[Evidence], list[str]]]:
    for error in catalogs.errors:
        yield f"errors/{error.id}", error.evidence, error.docs
    for surface in catalogs.surfaces:
        yield f"surfaces/{surface.id}", surface.evidence, surface.docs
    for variable in catalogs.variables:
        yield f"variables/{variable.name}", variable.evidence, []
    for capability in catalogs.capabilities:
        yield f"rbac/{capability.id}", [], capability.docs


def catalog_drift(catalogs: Catalogs, upstream_root: Path, doc_index: DocIndex) -> list[str]:
    """Problems with catalog references against an upstream checkout (empty when in sync)."""
    problems: list[str] = []
    for label, evidence, docs in _entries_with_references(catalogs):
        for item in evidence:
            path = upstream_root / Path(*item.file.split("/"))
            if not path.is_file():
                problems.append(f"{label}: evidence file {item.file} no longer exists upstream")
            elif item.contains not in path.read_text(encoding="utf-8", errors="replace"):
                problems.append(f"{label}: {item.file} no longer contains {item.contains!r}")
        for citation in docs:
            if doc_index.find(citation) is None:
                problems.append(f"{label}: documentation anchor {citation} no longer exists upstream")
    return problems
