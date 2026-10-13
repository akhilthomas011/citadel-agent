# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Shared state for tools: the runtime, drafts and the change engine of one agent session."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from citadel_agent.changes.drafts import DraftStore
from citadel_agent.changes.repository import CanonicalRepository, IntentRecord
from citadel_agent.changes.workspace import WorkspaceError
from citadel_agent.policy.admin import AdminPolicy, EnvironmentPolicy
from citadel_agent.proc import CommandError
from citadel_agent.tools.registry import ToolInputError


@dataclass
class ToolContext:
    runtime: Any
    drafts: DraftStore
    prepared: dict[str, list[str]] = field(default_factory=dict)
    _engine: Any = None
    _synced_at: float = 0.0
    sync_interval: float = 60.0

    def repository(self) -> CanonicalRepository:
        """The canonical repository at the latest base commit (synced at most once a minute)."""
        workspace = self.runtime.workspace
        if time.monotonic() - self._synced_at > self.sync_interval:
            try:
                workspace.sync_base()
            except (WorkspaceError, CommandError) as exc:
                raise ToolInputError(f"The contracts repository is not available: {exc}") from exc
            self._synced_at = time.monotonic()
        return workspace.repository

    def access_contracts(self, environment: str | None = None) -> list[IntentRecord]:
        records = self.repository().intents("access-contract")
        return [r for r in records if environment is None or r.intent.environment == environment]

    def publish_contracts(self, environment: str | None = None) -> list[IntentRecord]:
        records = self.repository().intents("publish-contract")
        return [r for r in records if environment is None or r.intent.environment == environment]

    def find_access_contract(
        self, business_unit: str, use_case: str, environment: str
    ) -> IntentRecord | None:
        for record in self.access_contracts(environment):
            use = record.intent.use_case
            if (
                use.business_unit.lower() == business_unit.lower()
                and use.use_case_name.lower() == use_case.lower()
            ):
                return record
        return None

    def owned_access_contract(self, business_unit: str, use_case: str, environment: str) -> Any:
        """A contract the caller owns (operators may use any). Raises when missing or not allowed."""
        record = self.find_access_contract(business_unit, use_case, environment)
        if record is None:
            raise ToolInputError(f"No access contract for {business_unit}/{use_case} in {environment}")
        if self.persona != "operator" and not self.runtime.authorizer.owns(record.intent):
            raise PermissionError("Only the contract's owners or the platform team can do this")
        return record.intent

    @property
    def persona(self) -> str:
        return str(self.runtime.caller.persona.value)

    @property
    def caller(self) -> str:
        return str(self.runtime.principal.label)

    @property
    def policy(self) -> AdminPolicy:
        policy = self.runtime.policy
        if policy is None:
            reason = self.runtime.loaded.error or "the admin policy is not available"
            raise ToolInputError(f"The agent is read-only: {reason}")
        return policy

    def environment_name(self, name: str | None = None) -> str:
        return name or self.runtime.settings.environment or self.policy.default_environment

    def environment(self, name: str | None = None) -> EnvironmentPolicy:
        env_name = self.environment_name(name)
        try:
            return self.policy.environment(env_name)
        except (KeyError, ValueError) as exc:
            known = ", ".join(sorted(self.policy.environments))
            raise ToolInputError(f"Unknown environment '{env_name}'. Known environments: {known}") from exc

    def engine(self) -> Any:
        if self._engine is None:
            self._engine = self.runtime.engine()
        return self._engine
