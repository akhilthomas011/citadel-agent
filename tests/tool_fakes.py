# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Shared fakes for tool tests: a tool context over in-memory access contracts."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

from citadel_agent.changes.drafts import DraftStore
from citadel_agent.changes.repository import IntentRecord
from citadel_agent.contracts import parse_intent
from citadel_agent.tools.context import ToolContext

GATEWAY = "https://gw.example"
CALLER = "dev@contoso.com"


def make_intent(bu: str = "HR", uc: str = "Chat", owners: list[str] | None = None, **policy: Any) -> Any:
    return parse_intent(
        {
            "kind": "access-contract",
            "environment": "dev",
            "metadata": {"owners": owners if owners is not None else [CALLER]},
            "useCase": {"businessUnit": bu, "useCaseName": uc, "environment": "DEV"},
            "services": [
                {
                    "code": "LLM",
                    "apis": ["unified-ai-api"],
                    "keySecretName": f"{bu}-{uc}-KEY",
                    "policy": {
                        "allowedModels": ["gpt-5.2"],
                        "tokenLimit": {"tokensPerMinute": 5000},
                        **policy,
                    },
                }
            ],
        }
    )


class FakeAuthorizer:
    def __init__(self, caps: list[str] | None = None) -> None:
        self.caps = caps or []

    def owns(self, intent: Any) -> bool:
        return CALLER in intent.metadata.owners

    def require(self, operation: Any) -> None:
        return None

    def require_owner(self, intent: Any) -> None:
        if not self.owns(intent):
            raise PermissionError("You are not an owner of this contract")

    def check_caps(self, intent: Any) -> list[str]:
        return self.caps


class FakeContext(ToolContext):
    def __init__(
        self, tmp: Path, records: list[Any], *, persona: str = "consumer", caps: list[str] | None = None
    ):
        env = SimpleNamespace(
            gateway_url=GATEWAY,
            key_vault=SimpleNamespace(name="kv-1"),
            log_analytics_workspace_id="0f8fad5b-d9cb-469f-a165-70867728950e",
            cosmos=SimpleNamespace(account_name="cosmos-1", database="ai-usage-db"),
        )
        policy = SimpleNamespace(
            default_environment="dev", environments={"dev": env}, environment=lambda n: env
        )
        runtime = SimpleNamespace(
            policy=policy,
            settings=SimpleNamespace(environment=None),
            loaded=SimpleNamespace(error=None),
            principal=SimpleNamespace(label=CALLER),
            caller=SimpleNamespace(persona=SimpleNamespace(value=persona)),
            authorizer=FakeAuthorizer(caps),
            credential=object(),
        )
        super().__init__(runtime=runtime, drafts=DraftStore(tmp / "drafts"))
        self.records = records

    def access_contracts(self, environment: str | None = None) -> list[IntentRecord]:
        return [r for r in self.records if environment is None or r.intent.environment == environment]


def make_record(intent: Any) -> IntentRecord:
    use = intent.use_case
    return IntentRecord(
        PurePosixPath(
            f"contracts/access/{use.business_unit.lower()}/{use.use_case_name.lower()}/dev/contract.yaml"
        ),
        intent,
    )
