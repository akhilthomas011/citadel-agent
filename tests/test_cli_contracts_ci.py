# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""CI commands must fail loudly: a failed what-if or pre-deploy check may never look like success."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from citadel_agent import cli_contracts
from citadel_agent.azure_clients.deployments import DeploymentRecord, WhatIfChange, WhatIfReport
from citadel_agent.changes.changeset import StepKind
from citadel_agent.cli import app

runner = CliRunner()


class FakeRunner:
    def __init__(self, report: WhatIfReport) -> None:
        self.report = report
        self.deployed: list[str] = []

    def what_if(self, target: Any, name: str, compiled: Any) -> WhatIfReport:
        return self.report

    def deploy(self, target: Any, name: str, compiled: Any) -> DeploymentRecord:
        self.deployed.append(name)
        return DeploymentRecord(name=name, provisioning_state="Succeeded")


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch):
    def install(report: WhatIfReport) -> FakeRunner:
        step = SimpleNamespace(
            id="access-a",
            kind=StepKind.DEPLOY,
            parameters_file="contracts/a/main.bicepparam",
            subscription_id="11111111-1111-1111-1111-111111111111",
            location="swedencentral",
            deployment_name="dep-a",
            parameters_hash="p",
            template_hash="t",
        )
        change = SimpleNamespace(id="cs-1", environment="dev", executed_by=None, ordered_steps=lambda: [step])
        fake = FakeRunner(report)
        monkeypatch.setattr(cli_contracts, "_load_manifest", lambda repo, manifest: change)
        monkeypatch.setattr(
            cli_contracts,
            "compile_deployment",
            lambda path: SimpleNamespace(parameters_hash="p", template_hash="t"),
        )
        monkeypatch.setattr(cli_contracts, "_runner", lambda subscription_id: fake)
        return fake

    return install


def test_whatif_with_changes_succeeds(stubbed, tmp_path: Path) -> None:
    ok = WhatIfReport(
        status="Succeeded",
        changes=(WhatIfChange("/subscriptions/s/resourceGroups/rg/providers/x/y/z", "Create"),),
    )
    stubbed(ok)

    result = runner.invoke(app, ["contracts", "whatif", "--repo", str(tmp_path), "--manifest", "m.json"])

    assert result.exit_code == 0, result.output
    assert "Create 1" in result.output


def test_whatif_error_is_not_reported_as_no_changes(stubbed, tmp_path: Path) -> None:
    stubbed(WhatIfReport(status="Failed", error="Authorization failed"))

    result = runner.invoke(app, ["contracts", "whatif", "--repo", str(tmp_path), "--manifest", "m.json"])

    assert result.exit_code == 1
    assert "what-if failed" in result.output
    assert "no changes" not in result.output
    assert "Authorization failed" in result.output


def test_deploy_stops_when_the_pre_deploy_whatif_fails(stubbed, tmp_path: Path) -> None:
    fake = stubbed(WhatIfReport(status="Failed", error="Authorization failed"))

    result = runner.invoke(app, ["contracts", "deploy", "--repo", str(tmp_path), "--manifest", "m.json"])

    assert result.exit_code == 1
    assert fake.deployed == []


def test_deploy_runs_after_a_clean_whatif(stubbed, tmp_path: Path) -> None:
    fake = stubbed(WhatIfReport(status="Succeeded"))

    result = runner.invoke(app, ["contracts", "deploy", "--repo", str(tmp_path), "--manifest", "m.json"])

    assert result.exit_code == 0, result.output
    assert fake.deployed == ["dep-a"]
