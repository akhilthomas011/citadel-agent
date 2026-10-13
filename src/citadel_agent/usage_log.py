# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Per-request model usage, so input-token cost can be measured instead of guessed.

Records contain counts and tool names only, never prompts, answers or tool output.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USAGE_FILE = "usage.jsonl"


def _count(details: Any, key: str) -> int:
    value = details.get(key) if isinstance(details, dict) else getattr(details, key, None)
    return int(value or 0)


@dataclass
class TurnUsage:
    """Usage of one user turn: one entry per model request."""

    requests: list[dict[str, int]] = field(default_factory=list)

    def add_update(self, update: Any) -> None:
        """Collect usage content from a streamed update (one per model request)."""
        for content in getattr(update, "contents", None) or []:
            if getattr(content, "type", None) == "usage":
                self.add_details(getattr(content, "usage_details", None))

    def add_details(self, details: Any) -> None:
        if not details:
            return
        self.requests.append(
            {
                "input": _count(details, "input_token_count"),
                "output": _count(details, "output_token_count"),
                "cached": _count(details, "cache_read_input_token_count"),
            }
        )

    @property
    def input_tokens(self) -> int:
        return sum(r["input"] for r in self.requests)

    @property
    def output_tokens(self) -> int:
        return sum(r["output"] for r in self.requests)

    def summary(self) -> str:
        if not self.requests:
            return ""
        calls = len(self.requests)
        return (
            f"tokens: {self.input_tokens:,} in, {self.output_tokens:,} out "
            f"({calls} model call{'s' if calls != 1 else ''}, largest request "
            f"{max(r['input'] for r in self.requests):,} in)"
        )


class UsageLog:
    """Append-only JSON Lines file under the agent's state directory."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(
        self,
        turn: TurnUsage,
        *,
        session_id: str,
        tools: Iterable[str],
        domains: Iterable[str],
        history_messages: int | None = None,
    ) -> None:
        if not turn.requests:
            return
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        tool_names = sorted(tools)
        lines = [
            json.dumps(
                {
                    "ts": stamp,
                    "session": session_id,
                    "call": index,
                    **request,
                    "tools": len(tool_names),
                    "domains": sorted(domains),
                    "history": history_messages,
                },
                separators=(",", ":"),
            )
            for index, request in enumerate(turn.requests, start=1)
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")


def summarize(path: Path, *, last: int = 0) -> dict[str, int]:
    """Totals and averages over a usage log, optionally the most recent ``last`` requests."""
    if not path.is_file():
        return {}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if last > 0:
        rows = rows[-last:]
    if not rows:
        return {}
    inputs = sorted(r["input"] for r in rows)
    return {
        "requests": len(rows),
        "input_total": sum(inputs),
        "output_total": sum(r["output"] for r in rows),
        "input_average": round(sum(inputs) / len(inputs)),
        "input_p90": inputs[min(len(inputs) - 1, int(len(inputs) * 0.9))],
        "input_max": inputs[-1],
    }
