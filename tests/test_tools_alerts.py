# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the alert rules tool (fakes; no Azure)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from citadel_agent.tools import alerts
from citadel_agent.tools.registry import ToolInputError
from tool_fakes import FakeContext, make_intent, make_record


class AlertContext(FakeContext):
    def __init__(self, tmp: Path, records: list[Any], *, persona: str = "consumer") -> None:
        super().__init__(tmp, records, persona=persona)
        self.runtime.authorizer.require = lambda operation: None

    def repository(self) -> Any:
        return SimpleNamespace(intents=lambda kind: [])


def test_use_case_alerts_also_enable_contract_events(tmp_path: Path) -> None:
    context = AlertContext(tmp_path, [make_record(make_intent())])
    output = alerts.draft_alert_rules(
        context,
        "team@contoso.com",
        business_unit="HR",
        use_case="Chat",
        events=["throttling", "auth-failure"],
    )
    assert "for LLM HR Chat DEV" in output and "auth-failure: more than 20 events" in output
    drafts = {d.intent.kind: d for d in context.drafts.list()}
    rules = drafts["alerts"].intent
    assert (
        rules.product_display_name_filter == "LLM HR Chat DEV"
        and rules.effective_name_prefix == "aigw-hr-chat"
    )
    toggles = drafts["access-contract"].intent.services[0].policy.alerts
    assert toggles.auth_failure and toggles.throttling


def test_default_events_need_no_contract_change(tmp_path: Path) -> None:
    context = AlertContext(tmp_path, [make_record(make_intent())])
    output = alerts.draft_alert_rules(context, "team@contoso.com", business_unit="HR", use_case="Chat")
    assert "does not emit" not in output and len(context.drafts.list()) == 1


def test_gateway_alerts_are_for_operators(tmp_path: Path) -> None:
    with pytest.raises(ToolInputError, match="platform team"):
        alerts.draft_alert_rules(AlertContext(tmp_path, []), "ops@contoso.com")
    operator = AlertContext(tmp_path, [], persona="operator")
    assert "for the whole gateway" in alerts.draft_alert_rules(operator, "ops@contoso.com")
    with pytest.raises(ToolInputError, match="not valid"):
        alerts.draft_alert_rules(operator, "not-an-email")
