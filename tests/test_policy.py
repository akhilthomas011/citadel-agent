# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the admin policy and authorization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from citadel_agent.azure_clients.arm import merge_permissions
from citadel_agent.azure_clients.credentials import PrincipalInfo
from citadel_agent.contracts import parse_intent
from citadel_agent.policy import (
    AdminPolicyError,
    AuthorizationError,
    Authorizer,
    Caller,
    ChangeMode,
    LoadedPolicy,
    Operation,
    Persona,
    PolicySource,
    derive_persona,
    load_admin_policy,
    load_bootstrap_policy,
    parse_admin_policy,
)
from citadel_agent.proc import run_command

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "admin-policy.example.yaml"
USER = PrincipalInfo(object_id="oid-1", tenant_id="t", display_name="Dev@Contoso.com", principal_type="User")


def example() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def access_intent(**policy: Any) -> Any:
    return parse_intent(
        {
            "kind": "access-contract",
            "environment": "dev",
            "metadata": {"owners": ["dev@contoso.com"]},
            "useCase": {"businessUnit": "HR", "useCaseName": "Chat", "environment": "DEV"},
            "services": [
                {"code": "LLM", "apis": ["universal-llm-api"], "keySecretName": "HR-KEY", "policy": policy}
            ],
        }
    )


def authorizer(persona: Persona, policy_data: dict[str, Any] | None = None) -> Authorizer:
    policy = parse_admin_policy(yaml.safe_dump(policy_data or example()))
    return Authorizer(LoadedPolicy(policy=policy, source=PolicySource.REPOSITORY), Caller(USER, persona))


def test_example_policy_is_valid() -> None:
    policy = parse_admin_policy(EXAMPLE.read_text(encoding="utf-8"))
    assert policy.environment("dev").mode is ChangeMode.DIRECT
    assert policy.environment("prod").mode is ChangeMode.PR
    target = policy.environment("dev").to_target("dev")
    assert target.environment_class == "dev" and target.gateway_url.startswith("https://")


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "direct"},
        {"allowPrerequisiteActions": True},
        {"caps": {"allowDirectOutputCredentials": True}},
    ],
)
def test_prod_hard_rules(change: dict[str, Any]) -> None:
    data = example()
    data["environments"]["prod"].update(change)
    with pytest.raises(AdminPolicyError, match="prod-class"):
        parse_admin_policy(yaml.safe_dump(data))


def test_unknown_operations_and_environments_are_rejected() -> None:
    data = example()
    data["operations"]["consumer"].append("delete.everything")
    with pytest.raises(AdminPolicyError, match="Unknown consumer operations"):
        parse_admin_policy(yaml.safe_dump(data))
    data = example()
    data["defaultEnvironment"] = "staging"
    with pytest.raises(AdminPolicyError, match="defaultEnvironment"):
        parse_admin_policy(yaml.safe_dump(data))


def _git(cwd: Path, *args: str) -> None:
    run_command("git", args, cwd=cwd)


def test_load_uses_committed_policy_not_working_copy(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "a@example.com")
    _git(tmp_path, "config", "user.name", "a")
    policy_file = tmp_path / ".citadel" / "admin-policy.yaml"
    policy_file.parent.mkdir()
    policy_file.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "policy")

    # A local, uncommitted edit that tries to make prod writable directly must be ignored.
    policy_file.write_text("garbage: [", encoding="utf-8")
    loaded = load_admin_policy(tmp_path)
    assert loaded.writable and loaded.source is PolicySource.REPOSITORY
    assert loaded.ref == "main" and loaded.blob_sha and len(loaded.blob_sha) == 40


def test_load_fails_closed(tmp_path: Path) -> None:
    loaded = load_admin_policy(tmp_path)
    assert not loaded.writable and loaded.error
    auth = Authorizer(loaded, Caller(USER, Persona.OPERATOR))
    with pytest.raises(AuthorizationError, match="admin policy is unavailable"):
        auth.require(Operation.ACCESS_WRITE)
    auth.require(Operation.MODELS_READ)  # reads remain available


def test_bootstrap_policy_only_allows_dev_environments(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    assert not load_bootstrap_policy(path).writable  # example contains a prod environment
    data = example()
    del data["environments"]["prod"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    loaded = load_bootstrap_policy(path)
    assert loaded.writable and loaded.source is PolicySource.LOCAL_BOOTSTRAP


def test_persona_is_derived_from_apim_permissions_or_groups() -> None:
    policy = parse_admin_policy(EXAMPLE.read_text(encoding="utf-8"))
    contributor = merge_permissions([{"actions": ["Microsoft.ApiManagement/service/*"]}])
    reader = merge_permissions([{"actions": ["*/read"]}])
    assert derive_persona(policy, contributor) is Persona.OPERATOR
    assert derive_persona(policy, reader) is Persona.CONSUMER
    assert derive_persona(None, contributor) is Persona.CONSUMER

    data = example()
    data["personas"]["operatorGroupObjectIds"] = ["11111111-1111-1111-1111-111111111111"]
    grouped = parse_admin_policy(yaml.safe_dump(data))
    assert derive_persona(grouped, reader, ["11111111-1111-1111-1111-111111111111"]) is Persona.OPERATOR


def test_consumer_operations_and_ownership() -> None:
    consumer = authorizer(Persona.CONSUMER)
    consumer.require(Operation.ACCESS_WRITE)
    with pytest.raises(AuthorizationError):
        consumer.require(Operation.BACKEND_WRITE)
    assert consumer.owns(access_intent())  # owner match is case-insensitive

    other = parse_intent(
        {**access_intent().model_dump(by_alias=True), "metadata": {"owners": ["x@contoso.com"]}}
    )
    assert not consumer.owns(other)
    assert authorizer(Persona.OPERATOR).owns(other)


def test_caps_are_enforced() -> None:
    auth = authorizer(Persona.CONSUMER)
    compliant = access_intent(
        tokenLimit={"tokensPerMinute": 1000, "tokenQuota": 1000000, "tokenQuotaPeriod": "Monthly"}
    )
    assert auth.check_caps(compliant) == []

    too_much = access_intent(
        tokenLimit={"tokensPerMinute": 999999, "tokenQuota": 9999999, "tokenQuotaPeriod": "Daily"}
    )
    problems = auth.check_caps(too_much)
    assert any("tokens/minute" in p for p in problems)
    assert any("month" in p for p in problems)

    unlimited = access_intent()
    assert any("token limit is required" in p for p in auth.check_caps(unlimited))
