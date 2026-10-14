# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Model pricing: validation, selection, planning and the seed command."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from citadel_agent import cli_pricing
from citadel_agent.cli import app
from citadel_agent.pricing import (
    PricingError,
    load_entries,
    plan_changes,
    sample_file,
    select_entries,
    validate_entry,
)

UPSTREAM = Path(__file__).resolve().parents[1] / "upstream" / "ai-hub-gateway"


def entry(model: str = "gpt-5.2", **overrides: Any) -> dict[str, Any]:
    base = {
        "id": "1",
        "model": model,
        "deploymentName": model,
        "isActive": True,
        "CostPerInputUnit": 1.25,
        "CostPerOutputUnit": 10,
        "CostPerCachedInputUnit": 0.13,
        "CostUnit": 1_000_000,
        "BaseCost": 0,
        "Currency": "USD",
        "CalculationMethod": "tokens",
        "region": "ALL",
    }
    return {**base, **overrides}


def test_a_valid_entry_has_no_problems() -> None:
    assert validate_entry(entry()) == []


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"CostUnit": 0}, "CostUnit"),
        ({"CostPerOutputUnit": -1}, "CostPerOutputUnit"),
        ({"CostPerInputUnit": "2"}, "CostPerInputUnit"),
        ({"isActive": "true"}, "isActive"),
        ({"CalculationMethod": "per-call"}, "CalculationMethod"),
        ({"model": ""}, "model"),
        ({"CalculationMethod": "percentage", "BaseCost": 0}, "BaseCost"),
    ],
)
def test_entries_that_would_misprice_usage_are_rejected(overrides: dict[str, Any], fragment: str) -> None:
    assert any(fragment in problem for problem in validate_entry(entry(**overrides)))


def test_only_models_the_gateway_serves_are_kept_case_insensitively() -> None:
    kept, skipped = select_entries([entry("gpt-5.2"), entry("GPT-4.1", id="2")], ["GPT-5.2", "other"])

    assert [e["model"] for e in kept] == ["gpt-5.2"]
    assert skipped == ["GPT-4.1"]
    assert len(select_entries([entry(), entry("x", id="2")], None)[0]) == 2


def test_a_new_price_keeps_its_id_unless_it_is_taken() -> None:
    plan = plan_changes(
        [entry("a", id="1"), entry("b", id="2")], [entry("z", id="1")], deployed_models=["a", "b"]
    )

    assert {e["model"]: e["id"] for e in plan.creates} == {"a": "2", "b": "3"}
    assert not plan.updates and not plan.unchanged


def test_an_existing_model_is_updated_in_place_and_never_duplicated() -> None:
    old = entry("gpt-5.2", id="7", CostPerInputUnit=1.0)

    plan = plan_changes([entry("gpt-5.2", id="1")], [old])

    assert plan.creates == [] and len(plan.updates) == 1
    existing, new = plan.updates[0]
    assert existing["CostPerInputUnit"] == 1.0 and new["CostPerInputUnit"] == 1.25 and new["id"] == "7"


def test_unchanged_prices_are_not_rewritten() -> None:
    plan = plan_changes([entry()], [entry()])

    assert plan.unchanged == ["gpt-5.2"] and plan.writes == []


def test_zero_priced_token_entries_are_placeholders_and_are_not_written() -> None:
    zero = entry("flux", id="2", CostPerInputUnit=0, CostPerOutputUnit=0, CostPerCachedInputUnit=0)

    plan = plan_changes([entry("a"), zero], [], deployed_models=["a", "flux"])

    assert plan.placeholders == ["flux"] and [e["model"] for e in plan.creates] == ["a"]
    assert plan.uncovered == ["flux"]
    assert "not written" in plan.render()


def test_a_placeholder_never_overwrites_a_real_price() -> None:
    zero = entry("a", CostPerInputUnit=0, CostPerOutputUnit=0, CostPerCachedInputUnit=0)

    plan = plan_changes([zero], [entry("a", id="9")], deployed_models=["a"])

    assert plan.writes == [] and plan.uncovered == []


def test_percentage_entries_are_not_placeholders() -> None:
    fixed = entry(
        "ptu", CalculationMethod="percentage", BaseCost=11000, CostPerInputUnit=0, CostPerOutputUnit=1
    )

    assert plan_changes([fixed], [])
    assert [e["model"] for e in plan_changes([fixed], []).creates] == ["ptu"]


def test_models_without_a_price_are_reported() -> None:
    plan = plan_changes([entry("a")], [], deployed_models=["a", "no-price"], skipped=["unused"])

    assert plan.uncovered == ["no-price"]
    text = plan.render()
    assert "no-price" in text and "unused" in text and "New prices: 1" in text


def test_a_bad_file_is_a_clear_error(tmp_path: Path) -> None:
    broken = tmp_path / "p.json"
    broken.write_text("{}", encoding="utf-8")
    with pytest.raises(PricingError, match="array"):
        load_entries(broken)
    with pytest.raises(PricingError, match="Cannot read"):
        load_entries(tmp_path / "missing.json")


@pytest.mark.skipif(not UPSTREAM.is_dir(), reason="upstream submodule is not checked out")
def test_the_upstream_sample_is_valid_and_matches_the_cost_report_schema() -> None:
    entries = load_entries(sample_file(UPSTREAM))

    assert len(entries) >= 10
    assert [p for e in entries for p in validate_entry(e)] == []


class FakeReader:
    instances: ClassVar[list[FakeReader]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.written: list[dict[str, Any]] = []
        FakeReader.instances.append(self)

    def discover_usage_database(self) -> str:
        return "usage-db"

    def query(self, database: str, container: str, sql: str, parameters: Any = None) -> list[dict[str, Any]]:
        return []

    def upsert(self, database: str, container: str, item: dict[str, Any]) -> None:
        assert (database, container) == ("usage-db", "model-pricing")
        self.written.append(item)


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    FakeReader.instances = []
    pricing = tmp_path / "prices.json"
    pricing.write_text(json.dumps([entry("gpt-5.2"), entry("unused", id="2")]), encoding="utf-8")

    def install(persona: str = "operator", cosmos: Any = "default"):
        if cosmos == "default":
            cosmos = SimpleNamespace(account_name="c", database=None)
        env = SimpleNamespace(cosmos=cosmos, gateway_url="https://gw.example")
        runtime = SimpleNamespace(
            caller=SimpleNamespace(persona=SimpleNamespace(value=persona)),
            environment=lambda: env,
            credential=object(),
            project_root=tmp_path,
            lock=SimpleNamespace(upstream=SimpleNamespace(path="upstream/x")),
        )
        monkeypatch.setattr(cli_pricing, "build_runtime", lambda settings: runtime)
        monkeypatch.setattr(cli_pricing, "CosmosUsageReader", FakeReader)
        monkeypatch.setattr(
            cli_pricing,
            "GatewayClient",
            lambda url: SimpleNamespace(
                backend_contract=lambda: SimpleNamespace(
                    model_names=lambda: {"gpt-5.2", "chat-default"},
                    aliases=lambda: [{"name": "chat-default"}],
                )
            ),
        )
        return pricing

    return install


def test_seed_previews_by_default_and_writes_nothing(seeded) -> None:
    pricing = seeded()

    result = CliRunner().invoke(app, ["pricing", "seed", "--file", str(pricing)])

    assert result.exit_code == 0, result.output
    assert "New prices: 1" in result.output and "unused" in result.output and "Preview only" in result.output
    assert "chat-default" not in result.output  # an alias is not a priced model
    assert "no price" not in result.output
    assert FakeReader.instances[0].written == []


def test_seed_writes_only_served_models_when_applied(seeded) -> None:
    pricing = seeded()

    result = CliRunner().invoke(app, ["pricing", "seed", "--file", str(pricing), "--apply"])

    assert result.exit_code == 0, result.output
    assert [w["model"] for w in FakeReader.instances[0].written] == ["gpt-5.2"]


def test_seed_is_an_operator_task_and_needs_cosmos(seeded) -> None:
    pricing = seeded(persona="consumer")
    refused = CliRunner().invoke(app, ["pricing", "seed", "--file", str(pricing), "--apply"])
    assert refused.exit_code == 1 and "operator" in refused.output
    assert FakeReader.instances == []

    pricing = seeded(cosmos=None)
    assert CliRunner().invoke(app, ["pricing", "seed", "--file", str(pricing)]).exit_code == 2


def test_seed_refuses_a_file_with_bad_prices(seeded, tmp_path: Path) -> None:
    seeded()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([entry(CostPerInputUnit=-5)]), encoding="utf-8")

    result = CliRunner().invoke(app, ["pricing", "seed", "--file", str(bad), "--apply"])

    assert result.exit_code == 2 and "CostPerInputUnit" in result.output
    assert FakeReader.instances == []  # rejected before Cosmos is touched
