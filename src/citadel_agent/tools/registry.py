# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tool registry: persona-aware tool exposure, approval modes and a safety wrapper for every tool.

Every tool runs through :func:`guarded`. It runs blocking work off the event loop, turns
exceptions into short error messages for the model (so the conversation can recover), and
redacts the output before the model sees it. Authorization is still re-checked inside the tools
and the change engine; hiding a tool from a persona is only the first layer.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from citadel_agent.safety.redaction import default_redactor
from citadel_agent.tools import TOOL_NAMES

logger = logging.getLogger(__name__)
PERSONAS = frozenset({"consumer", "operator"})
MAX_TOOL_OUTPUT = 8000


class ToolInputError(ValueError):
    """Raised by tools for problems the user or model can fix (shown without a stack trace)."""


def guarded(function: Callable[..., Any]) -> Callable[..., Any]:
    """Run a tool safely: off the event loop, redacted, with readable errors."""

    @functools.wraps(function)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        redactor = default_redactor()
        try:
            result = await asyncio.to_thread(function, *args, **kwargs)
        except (ToolInputError, PermissionError, ValueError) as exc:
            return redactor.redact(f"Error: {exc}")[:2000]
        except Exception as exc:
            # The model receives a short error; the traceback is only for debug logs.
            logger.debug("Tool %s failed", function.__name__, exc_info=True)
            return redactor.redact(f"Error: {type(exc).__name__}: {exc}")[:2000]
        text = result if isinstance(result, str) else str(result)
        text = redactor.redact(text)
        return text if len(text) <= MAX_TOOL_OUTPUT else text[:MAX_TOOL_OUTPUT] + "\n[output truncated]"

    return wrapper


@dataclass(frozen=True)
class ToolSpec:
    name: str
    function: Callable[..., Any]
    description: str
    personas: frozenset[str] = PERSONAS
    requires_approval: bool = False


@dataclass
class ToolRegistry:
    specs: dict[str, ToolSpec] = field(default_factory=dict)

    def add(self, spec: ToolSpec) -> None:
        if spec.name not in TOOL_NAMES:
            raise ValueError(f"Tool '{spec.name}' is not part of the tool vocabulary (citadel_agent.tools)")
        if spec.name in self.specs:
            raise ValueError(f"Tool '{spec.name}' is registered twice")
        if not spec.personas <= PERSONAS:
            raise ValueError(f"Tool '{spec.name}' has unknown personas {sorted(spec.personas - PERSONAS)}")
        self.specs[spec.name] = spec

    def for_persona(self, persona: str) -> list[ToolSpec]:
        return [spec for spec in self.specs.values() if persona in spec.personas]

    def build(self, persona: str) -> list[Any]:
        """Agent Framework function tools for ``persona``."""
        from agent_framework import tool

        return [
            tool(
                guarded(spec.function),
                name=spec.name,
                description=spec.description,
                approval_mode="always_require" if spec.requires_approval else "never_require",
            )
            for spec in self.for_persona(persona)
        ]
