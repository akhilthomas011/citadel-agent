# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the publish contract tool and its ordering with access contracts (fakes; no Azure)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from citadel_agent.agent import build_registry
from citadel_agent.changes.changeset import StepKind
from citadel_agent.changes.drafts import effective_dependencies
from citadel_agent.changes.repository import IntentRecord
from citadel_agent.contracts import parse_intent
from citadel_agent.contracts.publish import PublishContractIntent
from citadel_agent.policy.admin import Caps, Operation
from citadel_agent.policy.authz import cap_violations
from citadel_agent.tools import access, core, publish
from citadel_agent.tools.registry import ToolInputError
from test_engine import Harness, seed  # noqa: F401  (session fixture that seeds the engine repository)
from tool_fakes import FakeContext

SUB = "11111111-1111-1111-1111-111111111111"
PROJECT_ID = (
    f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/Microsoft.CognitiveServices"
    "/accounts/aif-1/projects/hr-project"
)
VAULT_URI = "https://kv-citadel.vault.azure.net/secrets/mcp-key"
PRINCIPAL = "22222222-3333-4444-5555-666666666666"


class FakeArm:
    def __init__(self, role: str | None = None) -> None:
        self.role = role

    def get(self, path: str, api_version: str) -> dict[str, Any]:
        if "roleDefinitions" in path:
            return {"properties": {"roleName": self.role}}
        return {"properties": {"principalId": PRINCIPAL}}

    def list(self, path: str, api_version: str, params: Any = None) -> list[dict[str, Any]]:
        definition = "/providers/Microsoft.Authorization/roleDefinitions/x"
        return [{"properties": {"roleDefinitionId": definition}}] if self.role else []


class PublishContext(FakeContext):
    def __init__(
        self,
        tmp: Path,
        *,
        persona: str = "operator",
        published: list[PublishContractIntent] | None = None,
        role: str | None = None,
    ):
        super().__init__(tmp, [], persona=persona)
        self.published = [
            IntentRecord(PurePosixPath(f"contracts/publish/{i.name}/dev/contract.yaml"), i)
            for i in published or []
        ]
        env = self.environment()
        env.subscription_id = SUB
        env.apim_managed_identity = SimpleNamespace(
            subscription_id=SUB, resource_group_name="rg", name="id-apim", client_id=SUB
        )
        self.runtime.arm = FakeArm(role)

    def publish_contracts(self, environment: str | None = None) -> list[IntentRecord]:
        return [r for r in self.published if environment is None or r.intent.environment == environment]


def published_intent(name: str = "other", path: str = "other-tool", asset_name: str = "other-tool") -> Any:
    return parse_intent(
        {
            "kind": "publish-contract",
            "environment": "dev",
            "name": name,
            "assets": [
                {
                    "assetType": "mcp-existing",
                    "name": asset_name,
                    "displayName": "Other",
                    "path": path,
                    "backend": {"url": "https://other.example/mcp"},
                }
            ],
        }
    )


def draft_id_of(output: str) -> str:
    match = re.search(r"draft_id: ([^\s).]+)", output)
    assert match
    return match.group(1)


def drafts_of(context: FakeContext) -> list[Any]:
    return context.drafts.list()


def test_operator_only_and_authorization_is_rechecked(tmp_path: Path) -> None:
    consumer = PublishContext(tmp_path / "c", persona="consumer")
    with pytest.raises(PermissionError, match="platform team"):
        publish.draft_publish_contract(
            consumer, "mcp-existing", "docs", backend_url="https://docs.example/mcp"
        )
    operator = PublishContext(tmp_path / "o")

    def deny(operation: Operation) -> None:
        assert operation is Operation.PUBLISH_WRITE
        raise PermissionError("not allowed")

    operator.runtime.authorizer.require = deny
    with pytest.raises(PermissionError, match="not allowed"):
        publish.draft_publish_contract(
            operator, "mcp-existing", "docs", backend_url="https://docs.example/mcp"
        )
    assert not drafts_of(operator) and not drafts_of(consumer)


def test_tool_is_registered_for_operators_only(tmp_path: Path) -> None:
    registry = build_registry(PublishContext(tmp_path))
    assert "draft_publish_contract" in {s.name for s in registry.for_persona("operator")}
    assert "draft_publish_contract" not in {s.name for s in registry.for_persona("consumer")}
    names = {t.name for t in registry.build("operator")}
    assert "draft_publish_contract" in names
    assert "draft_publish_contract" not in {t.name for t in registry.build("consumer")}


def test_mcp_from_api(tmp_path: Path) -> None:
    context = PublishContext(tmp_path)
    output = publish.draft_publish_contract(
        context, "mcp-from-api", "weather-tool", source_api="weather-api", operations=["get-forecast"]
    )
    assert "https://gw.example/mcp/weather-tool/mcp" in output and "(draft_id: " in output
    assert "must not require its own subscription key" in output
    draft = drafts_of(context)[0]
    asset = draft.intent.assets[0]
    assert draft.operation is Operation.PUBLISH_WRITE and draft.step_id == "publish-weather-tool"
    assert asset.source_api_name == "weather-api" and asset.operation_names == ["get-forecast"]
    assert draft.intent.metadata.owners == ["dev@contoso.com"] and not draft.prerequisites


def test_mcp_existing_with_key_vault_credentials(tmp_path: Path) -> None:
    context = PublishContext(tmp_path)
    output = publish.draft_publish_contract(
        context,
        "mcp-existing",
        "docs",
        backend_url="https://docs.example/mcp",
        auth_type="api-key-header",
        key_vault_secret_uri=VAULT_URI,
        auth_header_name="x-api-key",
        transport="sse",
    )
    assert "https://gw.example/mcp/docs" in output and "APIM subscription key required" in output
    backend = drafts_of(context)[0].intent.assets[0].backend
    assert backend.auth_type == "api-key-header"
    assert backend.auth_config.key_vault_secret_uri == VAULT_URI
    assert (
        backend.auth_config.named_value_key == "docs-key" and backend.auth_config.header_name == "x-api-key"
    )
    assert drafts_of(context)[0].intent.assets[0].transport_type == "sse"


def test_generic_a2a_defaults_to_the_foundry_audience(tmp_path: Path) -> None:
    context = PublishContext(tmp_path)
    output = publish.draft_publish_contract(
        context,
        "a2a",
        "hr-agent",
        backend_url="https://agents.example/a2a",
        agent_id="HR-Agent",
        agent_card_url="https://agents.example/a2a/card",
        auth_type="managed-identity",
    )
    asset = drafts_of(context)[0].intent.assets[0]
    assert asset.backend.auth_config.resource == "https://ai.azure.com" and asset.agent_id == "HR-Agent"
    assert "https://gw.example/agent/hr-agent" in output and "anonymous at the gateway" in output
    assert "A2A support is in preview" in output


def test_foundry_agent_records_manual_prerequisites(tmp_path: Path) -> None:
    context = PublishContext(tmp_path)
    output = publish.draft_publish_contract(
        context, "foundry-agent", "hr-chat-agent", foundry_project_id=PROJECT_ID, foundry_agent="HR-ChatAgent"
    )
    draft = drafts_of(context)[0]
    asset = draft.intent.assets[0]
    base = "https://aif-1.services.ai.azure.com/api/projects/hr-project/agents/HR-ChatAgent/endpoint/protocols/a2a"
    assert asset.backend.url == base and asset.agent_card_backend_url == f"{base}/agentCard/v1.0"
    assert asset.backend.auth_config.resource == "https://ai.azure.com" and asset.subscription_required
    rbac, patch = draft.prerequisites
    assert rbac["stepId"] == "foundry-rbac-hr-project"
    assert f"--assignee-object-id {PRINCIPAL}" in rbac["commands"][0]
    assert f"--scope {PROJECT_ID}" in rbac["commands"][0] and "Foundry Agent Consumer" in rbac["commands"][0]
    assert "az rest --method patch" in patch["commands"][0] and "a2a: {}" in patch["description"]
    assert output.count("Prerequisite (manual)") == 2 and "Nothing is deployed yet" in output


def test_foundry_role_prerequisite_is_skipped_when_already_granted(tmp_path: Path) -> None:
    context = PublishContext(tmp_path, role="Azure AI User")
    publish.draft_publish_contract(
        context, "foundry-agent", "hr-chat-agent", foundry_project_id=PROJECT_ID, foundry_agent="HR-ChatAgent"
    )
    assert [p["stepId"] for p in drafts_of(context)[0].prerequisites] == ["foundry-a2a-hr-chatagent"]


def test_foundry_role_prerequisite_is_kept_when_role_cannot_be_checked(tmp_path: Path) -> None:
    context = PublishContext(tmp_path)
    context.runtime.arm = SimpleNamespace()
    publish.draft_publish_contract(
        context, "foundry-agent", "hr-chat-agent", foundry_project_id=PROJECT_ID, foundry_agent="HR-ChatAgent"
    )
    rbac = drafts_of(context)[0].prerequisites[0]
    assert "<principal-id-of-the-gateway-managed-identity>" in rbac["commands"][0]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"asset_type": "mcp-from-api", "source_api": "weather-api"}, "needs: operations"),
        ({"asset_type": "mcp-existing"}, "needs: backend_url"),
        ({"asset_type": "a2a", "backend_url": "https://a.example"}, "needs: agent_id, agent_card_url"),
        (
            {"asset_type": "foundry-agent", "foundry_project_id": "/subscriptions/x"},
            "needs: foundry_agent",
        ),
        (
            {"asset_type": "foundry-agent", "foundry_project_id": "/subscriptions/x", "foundry_agent": "a"},
            "project's resource ID",
        ),
        (
            {
                "asset_type": "mcp-from-api",
                "source_api": "a",
                "operations": ["o"],
                "backend_url": "https://x.io",
            },
            "does not use: backend_url",
        ),
        ({"asset_type": "mcp-existing", "backend_url": "http://insecure.example"}, "backendUrl|backend.url"),
        (
            {
                "asset_type": "mcp-existing",
                "backend_url": "https://x.example",
                "auth_type": "managed-identity",
            },
            "authConfig.resource",
        ),
        (
            {"asset_type": "mcp-existing", "backend_url": "https://x.example", "auth_type": "api-key-bearer"},
            "key_vault_secret_uri",
        ),
        (
            {
                "asset_type": "mcp-existing",
                "backend_url": "https://x.example",
                "key_vault_secret_uri": VAULT_URI,
            },
            "api-key auth only",
        ),
        ({"asset_type": "bogus"}, "asset_type must be one of"),
    ],
)
def test_validation_errors_are_short_and_readable(
    tmp_path: Path, kwargs: dict[str, Any], message: str
) -> None:
    context = PublishContext(tmp_path)
    with pytest.raises(ToolInputError, match=message) as error:
        publish.draft_publish_contract(context, name="docs", **kwargs)
    assert len(str(error.value)) < 400 and not drafts_of(context)


def test_model_validation_errors_are_summarised(tmp_path: Path) -> None:
    context = PublishContext(tmp_path)
    with pytest.raises(ToolInputError, match=r"not valid: .*name"):
        publish.draft_publish_contract(
            context, "mcp-existing", "Bad Name", backend_url="https://x.example/mcp"
        )
    with pytest.raises(ToolInputError, match="not valid"):
        publish.draft_publish_contract(
            context,
            "mcp-existing",
            "docs",
            backend_url="https://x.example/mcp",
            path="docs/Upper",
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"backend_url": "https://x.example/mcp?code=0123456789abcdef"},
        {"backend_url": "https://user:hunter2@x.example/mcp"},
        {"backend_url": "https://x.example/mcp", "description": "key 0123456789abcdef0123456789abcdef"},
        {
            "backend_url": "https://x.example/mcp",
            "auth_type": "api-key-bearer",
            "key_vault_secret_uri": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIzNDU2Nzg5MA",
        },
        {
            "backend_url": "https://x.example/mcp",
            "auth_type": "api-key-header",
            "key_vault_secret_uri": "https://kv.example/not-a-vault-secret",
        },
    ],
)
def test_literal_secrets_are_rejected_without_echoing_them(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    context = PublishContext(tmp_path)
    with pytest.raises(ToolInputError) as error:
        publish.draft_publish_contract(context, "mcp-existing", "docs", **kwargs)
    text = str(error.value)
    assert not any(marker in text for marker in ("hunter2", "0123456789abcdef", "eyJhbGci"))
    assert not drafts_of(context)


def test_collisions_with_published_assets_and_drafts_are_rejected(tmp_path: Path) -> None:
    context = PublishContext(tmp_path, published=[published_intent()])
    with pytest.raises(ToolInputError, match="name other-tool is already published by contract other"):
        publish.draft_publish_contract(
            context, "mcp-existing", "other-tool", backend_url="https://x.example/mcp", path="mine"
        )
    with pytest.raises(ToolInputError, match="gateway path /mcp/other-tool is already used"):
        publish.draft_publish_contract(
            context, "mcp-existing", "mine", backend_url="https://x.example/mcp", path="other-tool"
        )
    with pytest.raises(ToolInputError, match="built-in LLM API"):
        publish.draft_publish_contract(
            context, "mcp-existing", "unified-ai-api", backend_url="https://x.example/mcp"
        )
    publish.draft_publish_contract(context, "mcp-existing", "mine", backend_url="https://x.example/mcp")
    with pytest.raises(ToolInputError, match="already published by draft d-"):
        publish.draft_publish_contract(
            context, "mcp-existing", "mine", contract_name="second", backend_url="https://x.example/mcp"
        )
    assert len(drafts_of(context)) == 1


def test_assets_are_added_to_a_contract_through_the_draft_or_the_repository(tmp_path: Path) -> None:
    context = PublishContext(tmp_path, published=[published_intent(name="shared")])
    with pytest.raises(ToolInputError, match="does not exist"):
        publish.draft_publish_contract(
            context, "mcp-existing", "a", backend_url="https://x.example", draft_id="d-missing-abcdef"
        )
    # Extending a contract that exists in the repository keeps its assets.
    first = publish.draft_publish_contract(
        context, "mcp-existing", "docs", contract_name="shared", backend_url="https://docs.example/mcp"
    )
    draft_id = draft_id_of(first)
    assert [a.name for a in drafts_of(context)[0].intent.assets] == ["other-tool", "docs"]
    with pytest.raises(ToolInputError, match=f"pass it as draft_id.*|{draft_id}"):
        publish.draft_publish_contract(
            context, "mcp-existing", "more", contract_name="shared", backend_url="https://x.example/mcp"
        )
    publish.draft_publish_contract(
        context,
        "foundry-agent",
        "hr-chat-agent",
        foundry_project_id=PROJECT_ID,
        foundry_agent="HR-ChatAgent",
        draft_id=draft_id,
    )
    updated = publish.draft_publish_contract(
        context,
        "mcp-existing",
        "docs",
        backend_url="https://docs.example/v2",
        draft_id=draft_id,
        description="v2",
    )
    assert "Updated MCP tool `docs`" in updated and len(drafts_of(context)) == 1
    draft = drafts_of(context)[0]
    assert [a.name for a in draft.intent.assets] == ["other-tool", "hr-chat-agent", "docs"]
    assert len(draft.prerequisites) == 2  # the Foundry steps survive revising the draft
    access_draft_id = draft_id_of(
        access.draft_access_contract(context, "HR", "Chat", models=["gpt-5.2"], tokens_per_minute=100)
    )
    with pytest.raises(ToolInputError, match="not a publish contract draft"):
        publish.draft_publish_contract(
            context, "mcp-existing", "a", backend_url="https://x.example", draft_id=access_draft_id
        )


def test_gateway_paths_must_be_unique_inside_a_contract(tmp_path: Path) -> None:
    context = PublishContext(tmp_path)
    first = publish.draft_publish_contract(
        context, "mcp-existing", "one", contract_name="pack", backend_url="https://x.example/mcp", path="same"
    )
    draft_id = draft_id_of(first)
    with pytest.raises(ToolInputError, match="gateway paths must be unique"):
        publish.draft_publish_contract(
            context,
            "mcp-existing",
            "two",
            backend_url="https://y.example/mcp",
            path="same",
            draft_id=draft_id,
        )


def test_preview_warning_names_the_release_track(tmp_path: Path) -> None:
    context = PublishContext(tmp_path)
    output = publish.draft_publish_contract(
        context, "mcp-existing", "docs", backend_url="https://docs.example/mcp"
    )
    assert "preview feature" in output and "publish-contract-version 1.1.0-preview" in output
    context.runtime.lock = SimpleNamespace(
        upstream=SimpleNamespace(release={"publish-contract-version": "1.2.0-preview"})
    )
    output = publish.draft_publish_contract(
        context, "mcp-existing", "docs2", backend_url="https://docs.example/mcp"
    )
    assert "publish-contract-version 1.2.0-preview" in output
    assert "draft_access_contract with tool_apis or agent_apis [docs2]" in output


# Publish -> access ordering ----------------------------------------------------------------


def access_draft(context: PublishContext, **kwargs: Any) -> str:
    return access.draft_access_contract(
        context, "HR", "Chat", models=["gpt-5.2"], tokens_per_minute=5000, asset_calls_per_minute=60, **kwargs
    )


def test_access_contract_grants_published_apis_through_a_multi_service(tmp_path: Path) -> None:
    context = PublishContext(tmp_path)
    publish.draft_publish_contract(context, "mcp-existing", "docs", backend_url="https://docs.example/mcp")
    publish.draft_publish_contract(
        context, "foundry-agent", "hr-agent", foundry_project_id=PROJECT_ID, foundry_agent="HR"
    )
    output = access_draft(context, tool_apis=["docs"], agent_apis=["hr-agent"])
    assert "Warning" not in output
    intent = next(d.intent for d in drafts_of(context) if d.intent.kind == "access-contract")
    llm, multi = intent.services
    assert llm.code == "LLM" and llm.apis == ["unified-ai-api", "universal-llm-api"]
    assert multi.code == "MULTI" and multi.apis == ["docs", "hr-agent"]
    assert multi.tool_apis == ["docs"] and multi.agent_apis == ["hr-agent"]
    assert multi.policy.tool_limits.calls == 60 and multi.policy.agent_limits.calls == 60


def test_access_to_unknown_published_apis_warns_and_needs_a_call_limit(tmp_path: Path) -> None:
    context = PublishContext(tmp_path)
    assert "Warning: ghost not found in publish contracts" in access_draft(context, tool_apis=["ghost"])
    with pytest.raises(ToolInputError, match="asset_calls_per_minute"):
        access.draft_access_contract(
            context, "HR", "Bot", models=["gpt-5.2"], tokens_per_minute=100, tool_apis=["ghost"]
        )
    with pytest.raises(ToolInputError, match="either a tool or an agent"):
        access_draft(context, tool_apis=["x"], agent_apis=["x"])


def test_token_caps_do_not_apply_to_a_service_that_only_grants_tools(tmp_path: Path) -> None:
    context = PublishContext(tmp_path)
    access_draft(context, tool_apis=["docs"])
    intent = next(d.intent for d in drafts_of(context) if d.intent.kind == "access-contract")
    caps = Caps(max_tokens_per_minute=50000)
    assert cap_violations(intent, caps) == []
    intent.services[1].apis.append("unified-ai-api")  # a MULTI service that also serves LLMs is still capped
    assert cap_violations(intent, caps) == ["MULTI: a token limit is required in this environment"]


def draft_pair(context: PublishContext) -> tuple[str, str]:
    publish_output = publish.draft_publish_contract(
        context, "mcp-existing", "ms-learn", backend_url="https://learn.example/mcp"
    )
    access_output = access_draft(context, tool_apis=["ms-learn"])
    return draft_id_of(publish_output), draft_id_of(access_output)


def test_access_steps_depend_on_the_publish_step_in_the_same_change(tmp_path: Path) -> None:
    context = PublishContext(tmp_path)
    publish_id, access_id = draft_pair(context)
    drafts = {d.id: d for d in drafts_of(context)}
    deps = effective_dependencies([drafts[access_id], drafts[publish_id]])
    assert deps == {access_id: ("publish-ms-learn",), publish_id: ()}
    # An access contract alone (the API is already published) does not depend on anything.
    assert effective_dependencies([drafts[access_id]]) == {access_id: ()}


def test_prepare_change_orders_publish_before_access_and_defers_what_if(
    seed: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(seed, repo)
    harness = Harness(repo, tmp_path)
    context = PublishContext(tmp_path / "ctx")
    context._engine = harness.engine
    publish_id, access_id = draft_pair(context)

    output = core.prepare_change(context, [access_id, publish_id], "Publish ms-learn and grant HR chat")
    change = harness.store.load(re.search(r"change_id: (\S+)", output).group(1))
    steps = {s.id: s for s in change.steps}
    publish_step = steps["publish-ms-learn"]
    access_step = next(s for s in change.steps if s.id.startswith("access-"))
    assert [s.id for s in change.ordered_steps()] == [publish_step.id, access_step.id]
    assert access_step.depends_on == ["publish-ms-learn"] and publish_step.depends_on == []
    assert publish_step.kind is access_step.kind is StepKind.DEPLOY
    assert publish_step.what_if is not None and not publish_step.what_if_deferred
    assert access_step.what_if is None and access_step.what_if_deferred
    assert "what-if: deferred until its dependencies are deployed" in output
    assert harness.runner.what_if_calls == [publish_step.deployment_name]


def test_prepare_change_does_not_defer_an_access_change_for_an_already_published_api(
    seed: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(seed, repo)
    harness = Harness(repo, tmp_path)
    context = PublishContext(tmp_path / "ctx", published=[published_intent(asset_name="ms-learn")])
    context._engine = harness.engine
    output = access_draft(context, tool_apis=["ms-learn"])
    assert "Warning" not in output
    access_id = drafts_of(context)[0].id
    prepared = core.prepare_change(context, [access_id], "Grant HR chat the published tool")
    change = harness.store.load(re.search(r"change_id: (\S+)", prepared).group(1))
    assert len(change.steps) == 1 and not change.steps[0].what_if_deferred
