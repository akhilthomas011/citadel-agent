# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Change sets: immutable, hashed descriptions of what the agent will change.

A change set is prepared (validated, rendered, compiled and previewed with what-if) before anyone
approves it. Approval binds to the change set's content hash, so anything that changes between
preview and execution (files, templates, upstream pin, admin policy, repository base) invalidates
the approval.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from citadel_agent.azure_clients.bicep import canonical_hash
from citadel_agent.contracts.common import CitadelModel


class StepKind(StrEnum):
    DEPLOY = "deploy"
    PREREQUISITE = "prerequisite"


class StepStatus(StrEnum):
    PENDING = "pending"
    DEPLOYED = "deployed"
    FAILED = "failed"
    SKIPPED = "skipped"
    MANUAL = "manual"


class WhatIfSummary(CitadelModel):
    """Predicted changes for a deploy step (resource IDs and property paths, never values)."""

    status: str
    counts: dict[str, int] = Field(default_factory=dict)
    fingerprint: str
    changes: list[str] = Field(default_factory=list)
    expected_noise: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    error: str | None = None


class ChangeStep(CitadelModel):
    """One node of the change-set DAG."""

    id: str = Field(pattern=r"^[a-z0-9-]{1,60}$")
    kind: StepKind
    description: str
    depends_on: list[str] = Field(default_factory=list)
    # deploy steps
    contract_dir: str | None = None
    template: str | None = None
    parameters_file: str | None = None
    location: str | None = None
    subscription_id: str | None = None
    deployment_name: str | None = None
    template_hash: str | None = None
    parameters_hash: str | None = None
    what_if: WhatIfSummary | None = None
    what_if_deferred: bool = False
    # prerequisite steps
    commands: list[str] = Field(default_factory=list)
    verification: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> ChangeStep:
        if self.kind is StepKind.DEPLOY:
            missing = [
                name
                for name in ("template", "parameters_file", "location", "subscription_id", "deployment_name")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(f"Deploy step '{self.id}' is missing: {', '.join(missing)}")
        return self


class CompatibilityTuple(CitadelModel):
    """Versions a change set was prepared against. Execution requires them to be unchanged."""

    agent_version: str
    agent_upstream_commit: str
    repo_upstream_commit: str
    live_release: dict[str, str] | None = None
    template_hashes: dict[str, str] = Field(default_factory=dict)


def new_change_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"cs-{stamp}-{secrets.token_hex(3)}"


class ChangeSet(CitadelModel):
    """An immutable plan of steps with everything needed to verify it before execution."""

    id: str = Field(pattern=r"^cs-\d{8}-\d{6}-[0-9a-f]{6}$")
    created_at: datetime
    created_by: str
    environment: str
    environment_class: str
    mode: Literal["direct", "pr"]
    summary: str
    intents: list[str]
    files: dict[str, str]
    steps: list[ChangeStep] = Field(min_length=1)
    repo_base_commit: str
    admin_policy_ref: str | None = None
    admin_policy_blob: str | None = None
    compat: CompatibilityTuple
    branch: str
    warnings: list[str] = Field(default_factory=list)
    executed_by: Literal["direct", "pipeline"] | None = None

    @model_validator(mode="after")
    def _dag(self) -> ChangeSet:
        self.ordered_steps()
        return self

    def ordered_steps(self) -> list[ChangeStep]:
        """Return the steps in dependency order (raises on unknown dependencies or cycles)."""
        by_id = {step.id: step for step in self.steps}
        if len(by_id) != len(self.steps):
            raise ValueError("Step ids must be unique")
        for step in self.steps:
            unknown = [dep for dep in step.depends_on if dep not in by_id]
            if unknown:
                raise ValueError(f"Step '{step.id}' depends on unknown steps: {', '.join(unknown)}")
        ordered: list[ChangeStep] = []
        state: dict[str, str] = {}

        def visit(step: ChangeStep) -> None:
            mark = state.get(step.id)
            if mark == "done":
                return
            if mark == "visiting":
                raise ValueError(f"Dependency cycle involving step '{step.id}'")
            state[step.id] = "visiting"
            for dep in step.depends_on:
                visit(by_id[dep])
            state[step.id] = "done"
            ordered.append(step)

        for step in self.steps:
            visit(step)
        return ordered

    def content_hash(self) -> str:
        """Hash of everything an approval covers (excludes execution metadata)."""
        payload = self.model_dump(mode="json", by_alias=True, exclude={"executed_by"})
        return canonical_hash(payload)

    def manifest(self) -> dict[str, object]:
        """The JSON manifest committed to ``.citadel/changesets/<id>.json`` (no secrets, no values)."""
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        data["contentHash"] = self.content_hash()
        return data
