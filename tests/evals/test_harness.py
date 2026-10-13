# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""The evaluation harness itself is deterministic and is tested without a model."""

from __future__ import annotations

import inspect
from pathlib import Path

from harness import Call, Outcome, stub_registry

from citadel_agent.agent import build_registry
from tool_fakes import FakeContext, make_intent, make_record


def _real(tmp_path: Path):
    return build_registry(FakeContext(tmp_path, [make_record(make_intent())], persona="operator"))


def test_stubs_keep_names_descriptions_schemas_and_approval_flags(tmp_path: Path) -> None:
    real = _real(tmp_path)
    stubbed = stub_registry(real, Outcome(), {})

    assert list(stubbed.specs) == list(real.specs)
    for name, spec in real.specs.items():
        stub = stubbed.specs[name]
        assert stub.description == spec.description
        assert stub.personas == spec.personas
        assert stub.requires_approval == spec.requires_approval
        assert inspect.signature(stub.function) == inspect.signature(spec.function)
    assert stubbed.specs["execute_change"].requires_approval


def test_stubs_record_calls_and_return_canned_answers(tmp_path: Path) -> None:
    outcome = Outcome()
    stubbed = stub_registry(
        _real(tmp_path), outcome, {"list_models": "MODELS", "explain_error": lambda a: "E"}
    )

    assert stubbed.specs["list_models"].function(environment="dev") == "MODELS"
    assert stubbed.specs["explain_error"].function(status=429) == "E"
    assert stubbed.specs["gateway_overview"].function() == "(stub) done."
    assert outcome.names() == ["list_models", "explain_error", "gateway_overview"]
    assert outcome.called("list_models")[0].arguments == {"environment": "dev"}


def test_no_stub_can_reach_azure(tmp_path: Path) -> None:
    """Stub bodies must not delegate to the real implementation."""
    real = _real(tmp_path)
    outcome = Outcome()
    stubbed = stub_registry(real, outcome, {})

    for name in ("execute_change", "prepare_change", "draft_access_contract", "usage_report"):
        assert stubbed.specs[name].function is not real.specs[name].function


def test_outcome_helpers() -> None:
    outcome = Outcome(calls=[Call("a", {}), Call("b", {"x": 1}), Call("a", {})], replies=["one", "two"])

    assert outcome.names() == ["a", "b", "a"]
    assert len(outcome.called("a")) == 2
    assert outcome.text == "one\ntwo"
