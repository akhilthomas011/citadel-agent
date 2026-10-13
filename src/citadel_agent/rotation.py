# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Zero-downtime key rotation as a durable, verified transaction.

Each access contract publishes one of its two subscription keys (``usePrimaryKey``); deploying
with ``keyRotationEnabled`` regenerates the other one. A rotation replaces the key consumers use:

1. switch: publish the other key (consumers move to it; the old key still works),
2. owners confirm every client uses the new key,
3. rotate: regenerate the old key (irreversible),
4. maintenance: turn regeneration off again (otherwise every deployment regenerates the key).

Rolling back is possible until step 3. The state lives in ``.citadel/rotations/<contract>.json``
in the canonical repository and is committed with each step. The next step is prepared only after
the previous deployment's outputs (``activeKeySlot``, ``keyRotationApplied``) are verified.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from citadel_agent.contracts.access import AccessContractIntent, KeyRotationState

ROTATIONS_DIR = ".citadel/rotations"
Phase = Literal["switch", "rotate", "maintenance", "rolled-back"]
Slot = Literal["primary", "secondary"]
# Phases that lock the contract against unrelated changes.
LOCKED_PHASES = frozenset({"switch", "rotate"})
NEXT_PHASE: dict[str, str] = {"switch": "rotate", "rotate": "maintenance"}


class RotationError(ValueError):
    """Raised when a rotation step is not allowed in the current state."""


class RotationState(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True)

    contract_dir: str
    environment: str
    phase: Phase
    from_slot: Slot
    to_slot: Slot
    started_by: str
    started_at: datetime
    updated_at: datetime

    @property
    def locked(self) -> bool:
        return self.phase in LOCKED_PHASES

    @property
    def finished(self) -> bool:
        return self.phase in ("maintenance", "rolled-back")


def rotation_path(contract_dir: str) -> str:
    slug = contract_dir.removeprefix("contracts/").strip("/").replace("/", "-")
    return f"{ROTATIONS_DIR}/{slug}.json"


def read_rotation_state(repo_root: Path, contract_dir: str) -> RotationState | None:
    path = repo_root / Path(*rotation_path(contract_dir).split("/"))
    if not path.is_file():
        return None
    return RotationState.model_validate_json(path.read_text(encoding="utf-8"))


def dump_state(state: RotationState) -> str:
    return json.dumps(state.model_dump(mode="json", by_alias=True), indent=2) + "\n"


def active_slot(intent: AccessContractIntent) -> Slot:
    return "primary" if intent.key_rotation.use_primary_key else "secondary"


def start(
    intent: AccessContractIntent, contract_dir: str, caller: str, existing: RotationState | None
) -> RotationState:
    """State for a new rotation of the key the contract currently publishes."""
    if existing is not None and existing.locked:
        raise RotationError(
            f"A rotation is already in progress (step {existing.phase}); advance or roll it back"
        )
    if intent.key_rotation.key_rotation_enabled:
        raise RotationError("Key regeneration is still switched on; deploy the maintenance step first")
    current = active_slot(intent)
    now = datetime.now(UTC)
    return RotationState(
        contract_dir=contract_dir,
        environment=intent.environment,
        phase="switch",
        from_slot=current,
        to_slot="secondary" if current == "primary" else "primary",
        started_by=caller,
        started_at=now,
        updated_at=now,
    )


def advance(state: RotationState, *, rollback: bool = False) -> RotationState:
    """The state of the next step (or of the rollback)."""
    if rollback:
        if state.phase != "switch":
            raise RotationError("Rolling back is only possible before the old key is regenerated")
        phase = "rolled-back"
    else:
        if state.phase not in NEXT_PHASE:
            raise RotationError(f"The rotation is finished (step {state.phase}); start a new one if needed")
        phase = NEXT_PHASE[state.phase]
    return state.model_copy(update={"phase": phase, "updated_at": datetime.now(UTC)})


def apply(intent: AccessContractIntent, state: RotationState) -> AccessContractIntent:
    """The contract intent for the state's phase."""
    if state.phase == "rolled-back":
        rotation = KeyRotationState(use_primary_key=state.from_slot == "primary", key_rotation_enabled=False)
    else:
        rotation = KeyRotationState(
            use_primary_key=state.to_slot == "primary", key_rotation_enabled=state.phase == "rotate"
        )
    return intent.model_copy(update={"key_rotation": rotation})


def expected_outputs(state: RotationState) -> dict[str, Any]:
    """Deployment outputs that prove the state's phase was applied."""
    if state.phase == "rolled-back":
        return {"activeKeySlot": state.from_slot, "keyRotationApplied": False}
    if state.phase == "switch":
        return {"activeKeySlot": state.to_slot, "keyRotationApplied": False}
    return {"activeKeySlot": state.to_slot, "keyRotationApplied": state.phase == "rotate"}


def verify(state: RotationState, outputs: dict[str, Any]) -> list[str]:
    """Differences between deployment outputs and what the phase requires (empty when verified)."""
    problems = []
    for name, expected in expected_outputs(state).items():
        actual = outputs.get(name)
        if isinstance(actual, dict):
            actual = actual.get("value")
        if actual != expected:
            problems.append(f"{name} is {actual!r}, expected {expected!r}")
    return problems
