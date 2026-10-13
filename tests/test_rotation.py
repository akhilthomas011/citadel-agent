# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for key rotation: the state machine and the rotation tools (fakes; no Azure)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from citadel_agent import rotation
from citadel_agent.azure_clients.deployments import DeploymentRecord
from citadel_agent.tools import rotation as rotation_tools
from citadel_agent.tools.registry import ToolInputError
from tool_fakes import FakeContext, make_intent, make_record

CONTRACT_DIR = "contracts/access/hr/chat/dev"


def _start(**rotation_flags: Any) -> rotation.RotationState:
    intent = make_intent()
    if rotation_flags:
        intent = intent.model_copy(update={"key_rotation": rotation.KeyRotationState(**rotation_flags)})
    return rotation.start(intent, CONTRACT_DIR, "dev@contoso.com", None)


def test_rotation_sequence_and_intents() -> None:
    state = _start()
    assert (state.phase, state.from_slot, state.to_slot) == (
        "switch",
        "primary",
        "secondary",
    ) and state.locked
    intent = make_intent()
    switched = rotation.apply(intent, state).key_rotation
    assert (switched.use_primary_key, switched.key_rotation_enabled) == (False, False)
    rotate = rotation.advance(state)
    assert rotate.phase == "rotate" and rotation.apply(intent, rotate).key_rotation.key_rotation_enabled
    maintenance = rotation.advance(rotate)
    assert maintenance.phase == "maintenance" and not maintenance.locked and maintenance.finished
    assert not rotation.apply(intent, maintenance).key_rotation.key_rotation_enabled
    with pytest.raises(rotation.RotationError, match="finished"):
        rotation.advance(maintenance)
    with pytest.raises(rotation.RotationError, match="before the old key"):
        rotation.advance(rotate, rollback=True)
    rolled = rotation.advance(state, rollback=True)
    assert rolled.phase == "rolled-back" and rotation.apply(intent, rolled).key_rotation.use_primary_key


def test_rotation_from_the_secondary_key_and_refusals() -> None:
    state = _start(use_primary_key=False, key_rotation_enabled=False)
    assert (state.from_slot, state.to_slot) == ("secondary", "primary")
    with pytest.raises(rotation.RotationError, match="maintenance"):
        _start(use_primary_key=True, key_rotation_enabled=True)
    with pytest.raises(rotation.RotationError, match="already in progress"):
        rotation.start(make_intent(), CONTRACT_DIR, "x", _start())


def test_verification_of_deployment_outputs() -> None:
    state = _start()
    assert (
        rotation.verify(state, {"activeKeySlot": {"value": "secondary"}, "keyRotationApplied": False}) == []
    )
    problems = rotation.verify(state, {"activeKeySlot": "primary", "keyRotationApplied": False})
    assert problems == ["activeKeySlot is 'primary', expected 'secondary'"]
    rotate = rotation.advance(state)
    assert rotation.verify(rotate, {"activeKeySlot": "secondary", "keyRotationApplied": True}) == []


def test_state_file_round_trip(tmp_path: Path) -> None:
    state = _start()
    path = tmp_path / rotation.rotation_path(CONTRACT_DIR)
    assert rotation.rotation_path(CONTRACT_DIR) == ".citadel/rotations/access-hr-chat-dev.json"
    path.parent.mkdir(parents=True)
    path.write_text(rotation.dump_state(state), encoding="utf-8")
    assert (
        json.loads(path.read_text(encoding="utf-8"))["contractDir"] == CONTRACT_DIR
    )  # read by repo validation
    assert rotation.read_rotation_state(tmp_path, CONTRACT_DIR) == state
    assert rotation.read_rotation_state(tmp_path, "contracts/access/hr/other/dev") is None


# Tools -----------------------------------------------------------------------------------------


class FakeEngine:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def prepare(self, request: Any) -> Any:
        self.requests.append(request)
        return SimpleNamespace(
            change=SimpleNamespace(id="cs-20261007-120000-abcdef"),
            content_hash="sha256:1",
            summary=lambda: "summary",
        )


class RotationContext(FakeContext):
    def __init__(self, tmp: Path, records: list[Any]) -> None:
        super().__init__(tmp, records)
        self.repo_root = tmp / "repo"
        self.repo_root.mkdir(exist_ok=True)
        self._engine = FakeEngine()

    def repository(self) -> Any:
        return SimpleNamespace(root=self.repo_root, path=lambda relative: self.repo_root / relative)

    def write_state(self, state: rotation.RotationState) -> None:
        path = self.repo_root / rotation.rotation_path(CONTRACT_DIR)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rotation.dump_state(state), encoding="utf-8")


def _deployed(**outputs: Any) -> DeploymentRecord:
    return DeploymentRecord(
        name="d", provisioning_state="Succeeded", outputs={k: {"value": v} for k, v in outputs.items()}
    )


def test_start_key_rotation_prepares_the_switch(tmp_path: Path) -> None:
    context = RotationContext(tmp_path, [make_record(make_intent())])
    text = rotation_tools.start_key_rotation(context, "HR", "Chat")
    assert "change_id: cs-20261007-120000-abcdef" in text and "owners_confirmed=true" in text
    request = context.engine().requests[0]
    change = request.changes[0]
    assert change.operation.value == "rotation.write" or "ROTATION" in change.operation.name
    assert change.intent.key_rotation.use_primary_key is False
    path, content = request.extra_files[0]
    assert path == ".citadel/rotations/access-hr-chat-dev.json" and '"phase": "switch"' in content
    assert "step 1 of 3" in request.summary


def test_advance_requires_verified_deployments_and_owner_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = RotationContext(tmp_path, [make_record(make_intent())])
    with pytest.raises(ToolInputError, match="No key rotation"):
        rotation_tools.advance_key_rotation(context, "HR", "Chat")
    state = _start()
    context.write_state(state)
    deployments: list[Any] = [None]
    monkeypatch.setattr(rotation_tools, "_deployment", lambda ctx, s: deployments[0])
    with pytest.raises(ToolInputError, match="not deployed yet"):
        rotation_tools.advance_key_rotation(context, "HR", "Chat")
    deployments[0] = _deployed(activeKeySlot="primary", keyRotationApplied=False)
    with pytest.raises(ToolInputError, match="expected result"):
        rotation_tools.advance_key_rotation(context, "HR", "Chat")
    deployments[0] = _deployed(activeKeySlot="secondary", keyRotationApplied=False)
    with pytest.raises(ToolInputError, match="owners_confirmed=true"):
        rotation_tools.advance_key_rotation(context, "HR", "Chat")
    rotation_tools.advance_key_rotation(context, "HR", "Chat", owners_confirmed=True)
    request = context.engine().requests[-1]
    assert request.changes[0].intent.key_rotation.key_rotation_enabled and "irreversible" in request.summary

    rollback = rotation_tools.advance_key_rotation(context, "HR", "Chat", rollback=True)
    assert (
        "change_id" in rollback
        and context.engine().requests[-1].changes[0].intent.key_rotation.use_primary_key
    )


def test_finished_rotation_reports_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RotationContext(tmp_path, [make_record(make_intent())])
    context.write_state(rotation.advance(rotation.advance(_start())))
    monkeypatch.setattr(
        rotation_tools,
        "_deployment",
        lambda ctx, s: _deployed(activeKeySlot="secondary", keyRotationApplied=False),
    )
    assert "complete and verified" in rotation_tools.advance_key_rotation(context, "HR", "Chat")
