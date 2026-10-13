# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Approval safeguards for the DevUI, which has less approval UI than the terminal chat.

The terminal chat renders the stored change set in its own approval prompt and asks for a typed
word before an irreversible step. The DevUI shows an approval card with only the raw call, and it
cannot ask for typed confirmation. These middleware close both gaps:

* ``ApprovalSummaryMiddleware`` puts the stored change set (not the model's description of it) in
  the conversation right before the approval card.
* ``IrreversibleStepGuard`` refuses an irreversible step that was approved in the DevUI, so the
  step can only run from the terminal chat where the user types the confirmation word.
"""

from __future__ import annotations

from collections.abc import Callable, MutableSequence
from typing import Any

from agent_framework import AgentMiddleware, Content, FunctionMiddleware

from citadel_agent.cli_chat import describe_approval, parse_arguments, typed_confirmation
from citadel_agent.tools.context import ToolContext

EXECUTE = "execute_change"
CHAT_ONLY = (
    "This step permanently replaces a key, and clients still using the old key will fail. It needs a "
    "typed confirmation that the DevUI cannot ask for. Reject this request and approve the step in "
    "`citadel-agent chat`."
)


def _arguments(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    return parse_arguments(value)


def approval_notice(ctx: ToolContext, arguments: dict[str, Any]) -> str:
    """The text shown before an execute_change approval card."""
    text, refuse = describe_approval(ctx, EXECUTE, arguments)
    parts = [
        "\n\n---\n**Change awaiting your approval** (read from the stored change set, not the model)\n\n",
        text,
    ]
    if refuse:
        parts.append("\n\n**Reject this request.** It does not match a stored change set.")
    elif typed_confirmation(ctx, arguments):
        parts.append(f"\n\n**Do not approve here.** {CHAT_ONLY}")
    parts.append("\n\n---\n")
    return "".join(parts)


def _is_execute_request(content: Any) -> bool:
    call = getattr(content, "function_call", None)
    return (
        getattr(content, "type", None) == "function_approval_request" and getattr(call, "name", "") == EXECUTE
    )


def annotate_contents(ctx: ToolContext, contents: MutableSequence[Any]) -> bool:
    """Insert the stored-change notice before each execute_change approval request."""
    changed = False
    for index in range(len(contents) - 1, -1, -1):
        content = contents[index]
        if _is_execute_request(content):
            notice = approval_notice(ctx, _arguments(getattr(content.function_call, "arguments", None)))
            contents.insert(index, Content.from_text(text=notice))
            changed = True
    return changed


class ApprovalSummaryMiddleware(AgentMiddleware):
    """Shows the stored change set to the user before every execute_change approval request."""

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    def _annotate_update(self, update: Any) -> Any:
        annotate_contents(self._ctx, update.contents)
        return update

    async def process(self, context: Any, call_next: Callable[[], Any]) -> None:
        # Streaming results are annotated update by update, once the stream is created.
        context.stream_transform_hooks.append(self._annotate_update)
        await call_next()
        if not context.stream and context.result is not None:
            for message in getattr(context.result, "messages", []) or []:
                annotate_contents(self._ctx, message.contents)


class IrreversibleStepGuard(FunctionMiddleware):
    """Refuses an irreversible change that was approved without the typed confirmation."""

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    async def process(self, context: Any, call_next: Callable[[], Any]) -> None:
        if context.function.name == EXECUTE and typed_confirmation(self._ctx, _arguments(context.arguments)):
            context.result = f"Error: not executed. {CHAT_ONLY}"
            return
        await call_next()


def devui_middleware(ctx: ToolContext) -> list[Any]:
    return [ApprovalSummaryMiddleware(ctx), IrreversibleStepGuard(ctx)]
