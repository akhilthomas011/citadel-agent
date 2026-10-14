# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Model pricing for cost reports: validate, select and plan entries for the ``model-pricing`` container.

Prices drive chargeback numbers, so nothing here decides a price. The entries come from a file a
person reviewed (by default upstream's sample, which lists public prices), and the planner only
checks their shape, keeps the ones for models the gateway really serves, and shows what would
change before anything is written.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

METHODS = ("tokens", "percentage")
UNIT_FIELDS = (
    "CostPerInputUnit",
    "CostPerOutputUnit",
    "CostPerCachedInputUnit",
    "CostPerAudioInputUnit",
    "CostPerCachedAudioInputUnit",
    "CostPerAudioOutputUnit",
    "CostPerReasoningOutputUnit",
    "CostPerImageInputUnit",
    "CostPerCachedImageInputUnit",
)
SAMPLE_PATH = ("src", "usage-reports", "model-pricing-generated-extended.json")
# Compared fields: everything a report reads. ``id`` and ``region`` identify the document.
COMPARED = ("model", "deploymentName", "isActive", "CostUnit", "BaseCost", "Currency", "CalculationMethod")


class PricingError(ValueError):
    """Raised when a pricing file cannot be used."""


def sample_file(upstream_root: Path) -> Path:
    return upstream_root.joinpath(*SAMPLE_PATH)


def load_entries(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PricingError(f"Cannot read pricing file {path}: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise PricingError("A pricing file must be a JSON array of objects")
    return data


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def validate_entry(entry: Mapping[str, Any]) -> list[str]:
    """Problems that would make a report price usage wrongly or crash it."""
    problems = []
    label = str(entry.get("model") or entry.get("id") or "?")
    for key in ("id", "model", "deploymentName", "Currency"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            problems.append(f"{label}: '{key}' must be a non-empty string")
    if not isinstance(entry.get("isActive"), bool):
        problems.append(f"{label}: 'isActive' must be true or false")
    if entry.get("CalculationMethod") not in METHODS:
        problems.append(f"{label}: 'CalculationMethod' must be one of {', '.join(METHODS)}")
    unit = entry.get("CostUnit")
    if not _number(unit) or unit <= 0:
        problems.append(f"{label}: 'CostUnit' must be a positive number")
    for key in (*UNIT_FIELDS, "BaseCost"):
        value = entry.get(key, 0)
        if not _number(value) or value < 0:
            problems.append(f"{label}: '{key}' must be a number that is not negative")
    if entry.get("CalculationMethod") == "percentage" and not entry.get("BaseCost"):
        problems.append(f"{label}: a 'percentage' entry needs a BaseCost to distribute")
    return problems


def select_entries(
    entries: Iterable[Mapping[str, Any]], deployed_models: Iterable[str] | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Entries for models the gateway serves, and the names of those skipped.

    ``deployed_models`` None keeps every entry.
    """
    wanted = None if deployed_models is None else {m.lower() for m in deployed_models}
    kept: list[dict[str, Any]] = []
    skipped: list[str] = []
    for entry in entries:
        names = {str(entry.get("model", "")).lower(), str(entry.get("deploymentName", "")).lower()}
        if wanted is None or names & wanted:
            kept.append(dict(entry))
        else:
            skipped.append(str(entry.get("model")))
    return kept, skipped


@dataclass
class PricingPlan:
    creates: list[dict[str, Any]] = field(default_factory=list)
    updates: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)  # (existing, new)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)  # priced at zero: not written
    uncovered: list[str] = field(default_factory=list)  # deployed models with no price entry

    @property
    def writes(self) -> list[dict[str, Any]]:
        return [*self.creates, *(new for _, new in self.updates)]

    def render(self) -> str:
        lines = [f"* New prices: {len(self.creates)}"]
        lines += [f"  * {e['model']}: {_describe(e)}" for e in self.creates]
        lines.append(f"* Prices that would change: {len(self.updates)}")
        for old, new in self.updates:
            lines.append(f"  * {new['model']}: {_describe(old)} -> {_describe(new)}")
        lines.append(f"* Already up to date: {len(self.unchanged)}")
        if self.placeholders:
            lines.append(
                f"* Priced at zero in the file, not written (a zero would show as a cost of 0): "
                f"{', '.join(self.placeholders)}"
            )
        if self.skipped:
            lines.append(f"* Not in the gateway, left out: {', '.join(self.skipped)}")
        if self.uncovered:
            lines.append(
                f"* Deployed with no price (usage shows, cost stays unknown): {', '.join(self.uncovered)}"
            )
        return "\n".join(lines)


def is_placeholder(entry: Mapping[str, Any]) -> bool:
    """A token-priced entry whose unit costs are all zero: a gap in the file, not a free model."""
    return entry.get("CalculationMethod") == "tokens" and not any(entry.get(k) for k in UNIT_FIELDS)


def _describe(entry: Mapping[str, Any]) -> str:
    if entry.get("CalculationMethod") == "percentage":
        return f"fixed {entry.get('BaseCost')} {entry.get('Currency')} shared by usage"
    unit = entry.get("CostUnit")
    return (
        f"in {entry.get('CostPerInputUnit')}, cached {entry.get('CostPerCachedInputUnit')}, "
        f"out {entry.get('CostPerOutputUnit')} {entry.get('Currency')} per {unit:,.0f} tokens"
    )


def _same(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    keys = (*COMPARED, *UNIT_FIELDS)
    return all(
        a.get(k, 0 if k in UNIT_FIELDS or k == "BaseCost" else None)
        == b.get(k, 0 if k in UNIT_FIELDS or k == "BaseCost" else None)
        for k in keys
    )


def plan_changes(
    entries: Iterable[Mapping[str, Any]],
    existing: Iterable[Mapping[str, Any]],
    *,
    skipped: Iterable[str] = (),
    deployed_models: Iterable[str] = (),
) -> PricingPlan:
    """What writing ``entries`` would do to the container.

    An existing document for the same model is updated in place (keeping its id) and never
    duplicated. A new document keeps its id when that id is free.
    """
    plan = PricingPlan(skipped=sorted(set(skipped)))
    by_model = {str(doc.get("model", "")).lower(): dict(doc) for doc in existing}
    taken = {str(doc.get("id")) for doc in by_model.values()}
    for entry in entries:
        entry = dict(entry)
        if is_placeholder(entry):
            plan.placeholders.append(entry["model"])
            continue
        current = by_model.get(str(entry["model"]).lower())
        if current is not None:
            entry["id"] = current["id"]
            if _same(current, entry):
                plan.unchanged.append(entry["model"])
            else:
                plan.updates.append((current, {**current, **entry}))
            continue
        if entry["id"] in taken:
            entry["id"] = str(max((int(i) for i in taken if str(i).isdigit()), default=0) + 1)
        taken.add(entry["id"])
        plan.creates.append(entry)
    priced = (
        {str(e["model"]).lower() for e in [*plan.creates, *(n for _, n in plan.updates)]}
        | {m.lower() for m in plan.unchanged}
        | {model for model, doc in by_model.items() if not is_placeholder(doc)}
    )
    plan.uncovered = sorted(m for m in set(deployed_models) if m.lower() not in priced)
    return plan
