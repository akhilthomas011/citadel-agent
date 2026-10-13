# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""The DevUI approval safeguards: stored change shown before approval, irreversible steps refused."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_framework import AgentResponse, Content, Message
from agent_framework._middleware import FunctionMiddlewarePipeline

from citadel_agent.approval_ui import (
    ApprovalSummaryMiddleware,
    IrreversibleStepGuard,
    annotate_contents,
    approval_notice,
    devui_middleware,
)
from test_agent import _change, _context


def _request(name: str, arguments: dict[str, Any]) -> Content:
    call = Content.from_function_call(call_id="call-1", name=name, arguments=arguments)
    return Content.from_function_approval_request(id="call-1", function_call=call)


def _stored(tmp_path: Path, *, irreversible: bool = False):
    context = _context(tmp_path)
    change = _change()
    snapshot = {".citadel/rotations/hr-chat.json": '{"phase": "rotate"}'} if irreversible else {}
    context.runtime.store.save(change, snapshot)
    return context, change


def _texts(contents: list[Content]) -> list[str]:
    return [c.text for c in contents if c.type == "text"]


def test_the_stored_change_is_shown_before_the_approval_card(tmp_path: Path) -> None:
    context, change = _stored(tmp_path)
    args = {"change_id": change.id, "approval_hash": change.content_hash()}
    contents = [Content.from_text(text="Shall I deploy?"), _request("execute_change", args)]

    assert annotate_contents(context, contents) is True

    assert [c.type for c in contents] == ["text", "text", "function_approval_request"]
    notice = _texts(contents)[1]
    assert "Onboard HR chat" in notice and "deploy this change now" in notice
    assert "stored change set" in notice and "Reject" not in notice


def test_a_request_that_does_not_match_the_stored_change_is_flagged(tmp_path: Path) -> None:
    context, change = _stored(tmp_path)

    notice = approval_notice(context, {"change_id": change.id, "approval_hash": "sha256:other"})

    assert "does not match" in notice and "Reject this request" in notice


def test_other_approval_requests_are_left_alone(tmp_path: Path) -> None:
    context, _ = _stored(tmp_path)
    contents = [_request("some_other_tool", {"a": 1})]

    assert annotate_contents(context, contents) is False
    assert len(contents) == 1


def test_irreversible_steps_are_flagged_and_refused_in_the_devui(tmp_path: Path) -> None:
    context, change = _stored(tmp_path, irreversible=True)
    args = {"change_id": change.id, "approval_hash": change.content_hash()}

    assert "Do not approve here" in approval_notice(context, args)

    guard = IrreversibleStepGuard(context)
    ran: list[bool] = []

    async def final(ctx: Any) -> str:
        ran.append(True)
        return "executed"

    fctx = SimpleNamespace(function=SimpleNamespace(name="execute_change"), arguments=args, result=None)
    result = asyncio.run(FunctionMiddlewarePipeline(guard).execute(fctx, final))

    assert not ran and "citadel-agent chat" in result


def test_ordinary_changes_still_execute(tmp_path: Path) -> None:
    context, change = _stored(tmp_path)
    args = {"change_id": change.id, "approval_hash": change.content_hash()}
    ran: list[bool] = []

    async def final(ctx: Any) -> str:
        ran.append(True)
        return "executed"

    guard = IrreversibleStepGuard(context)
    fctx = SimpleNamespace(function=SimpleNamespace(name="execute_change"), arguments=args, result=None)
    assert asyncio.run(FunctionMiddlewarePipeline(guard).execute(fctx, final)) == "executed" and ran

    other = SimpleNamespace(function=SimpleNamespace(name="list_models"), arguments={}, result=None)
    assert asyncio.run(FunctionMiddlewarePipeline(guard).execute(other, final)) == "executed"


def test_streamed_updates_are_annotated_through_the_registered_hook(tmp_path: Path) -> None:
    context, change = _stored(tmp_path)
    middleware = ApprovalSummaryMiddleware(context)
    args = {"change_id": change.id, "approval_hash": change.content_hash()}
    agent_context = SimpleNamespace(stream=True, result=None, stream_transform_hooks=[])

    async def call_next() -> None:
        return None

    asyncio.run(middleware.process(agent_context, call_next))
    update = SimpleNamespace(contents=[_request("execute_change", args)])
    (hook,) = agent_context.stream_transform_hooks
    hook(update)

    assert [c.type for c in update.contents] == ["text", "function_approval_request"]


def test_non_streamed_responses_are_annotated(tmp_path: Path) -> None:
    context, change = _stored(tmp_path)
    args = {"change_id": change.id, "approval_hash": change.content_hash()}
    response = AgentResponse(
        messages=[Message(role="assistant", contents=[_request("execute_change", args)])]
    )
    agent_context = SimpleNamespace(stream=False, result=response, stream_transform_hooks=[])

    async def call_next() -> None:
        return None

    asyncio.run(ApprovalSummaryMiddleware(context).process(agent_context, call_next))

    assert "Onboard HR chat" in " ".join(_texts(response.messages[0].contents))


def test_devui_gets_both_safeguards(tmp_path: Path) -> None:
    context, _ = _stored(tmp_path)

    kinds = {type(m).__name__ for m in devui_middleware(context)}

    assert kinds == {"ApprovalSummaryMiddleware", "IrreversibleStepGuard"}
