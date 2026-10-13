# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Contract drafts: validated intents waiting to be prepared as a change set.

The ``draft_*`` tools turn a conversation into a validated contract intent and save it here. The
``prepare_change`` tool turns one or more drafts into a change set. Drafts never touch Azure or the
canonical repository.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from citadel_agent.contracts.access import AccessContractIntent
from citadel_agent.contracts.intent_io import ContractIntent, dump_intent, intent_directory, parse_intent
from citadel_agent.contracts.publish import PublishContractIntent
from citadel_agent.policy.admin import Operation

_DRAFT_ID = re.compile(r"^d-[a-z0-9-]{1,60}-[0-9a-f]{6}$")
_STEP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


class DraftError(ValueError):
    """Raised when a draft does not exist or is invalid."""


@dataclass(frozen=True)
class Draft:
    id: str
    operation: Operation
    step_id: str
    depends_on: tuple[str, ...]
    created_by: str
    created_at: datetime
    intent: ContractIntent
    # Manual steps outside the contracts (for example a role assignment), as dictionaries with
    # stepId, description, commands and verification.
    prerequisites: tuple[dict[str, Any], ...] = ()

    @property
    def directory(self) -> str:
        return str(intent_directory(self.intent))


def _slug(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")[:40] or "draft"


class DraftStore:
    """Drafts stored as YAML files under the agent's state directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, draft_id: str) -> Path:
        if not _DRAFT_ID.match(draft_id):
            raise DraftError(f"'{draft_id}' is not a draft ID")
        return self.directory / f"{draft_id}.yaml"

    def save(
        self,
        intent: ContractIntent,
        *,
        operation: Operation,
        created_by: str,
        step_id: str | None = None,
        depends_on: tuple[str, ...] = (),
        draft_id: str | None = None,
        prerequisites: tuple[dict[str, Any], ...] = (),
    ) -> Draft:
        """Save a new draft, or replace ``draft_id`` with a revised intent."""
        directory = str(intent_directory(intent))
        draft_id = draft_id or f"d-{_slug(directory.removeprefix('contracts/'))}-{secrets.token_hex(3)}"
        step_id = step_id or _slug(f"{intent.kind.removesuffix('-contract')}-{directory.split('/')[-2]}")
        if not _STEP_ID.match(step_id):
            raise DraftError(f"Invalid step ID '{step_id}'")
        draft = Draft(
            id=draft_id,
            operation=operation,
            step_id=step_id,
            depends_on=tuple(depends_on),
            created_by=created_by,
            created_at=datetime.now(UTC),
            intent=intent,
            prerequisites=tuple(prerequisites),
        )
        path = self._path(draft_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "draft": {
                "id": draft.id,
                "operation": draft.operation.value,
                "stepId": draft.step_id,
                "dependsOn": list(draft.depends_on),
                "createdBy": draft.created_by,
                "createdAt": draft.created_at.isoformat(),
                "prerequisites": [dict(p) for p in draft.prerequisites],
            },
            "intent": yaml.safe_load(dump_intent(intent)),
        }
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
        return draft

    def load(self, draft_id: str) -> Draft:
        path = self._path(draft_id)
        if not path.is_file():
            raise DraftError(f"Draft {draft_id} does not exist")
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        meta = document["draft"]
        return Draft(
            id=meta["id"],
            operation=Operation(meta["operation"]),
            step_id=meta["stepId"],
            depends_on=tuple(meta.get("dependsOn") or ()),
            created_by=meta["createdBy"],
            created_at=datetime.fromisoformat(meta["createdAt"]),
            intent=parse_intent(document["intent"]),
            prerequisites=tuple(meta.get("prerequisites") or ()),
        )

    def list(self) -> list[Draft]:
        if not self.directory.is_dir():
            return []
        drafts = [self.load(path.stem) for path in self.directory.glob("d-*.yaml")]
        return sorted(drafts, key=lambda d: d.created_at)

    def delete(self, draft_id: str) -> None:
        self._path(draft_id).unlink(missing_ok=True)


def effective_dependencies(drafts: Sequence[Draft]) -> dict[str, tuple[str, ...]]:
    """Step dependencies per draft ID for one change set.

    Explicit dependencies are kept when their step is part of the set. An access contract that
    grants an API published by a publish draft in the same set also depends on that publish step:
    the gateway does not know the API until the publish step has been deployed.
    """
    present = {draft.step_id for draft in drafts}
    published: dict[str, str] = {}
    for draft in drafts:
        if isinstance(draft.intent, PublishContractIntent):
            published.update({asset.name: draft.step_id for asset in draft.intent.assets})
    dependencies: dict[str, tuple[str, ...]] = {}
    for draft in drafts:
        steps = [dep for dep in draft.depends_on if dep in present and dep != draft.step_id]
        if isinstance(draft.intent, AccessContractIntent):
            granted = {api for service in draft.intent.services for api in service.apis}
            steps.extend(step for api, step in published.items() if api in granted)
        dependencies[draft.id] = tuple(dict.fromkeys(steps))
    return dependencies
