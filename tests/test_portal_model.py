"""Exercise the actual Agent Framework/Azure OpenAI adapter with an in-process HTTP transport."""

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import openai
import pytest
from azure.core.credentials import AccessToken

from citadel_agent.changes.store import ChangeStore
from citadel_agent.knowledge.docs import DocIndex
from citadel_agent.portal.assistant import PortalAssistantService
from citadel_agent.portal.contracts import PortalRole
from test_portal_product import NOW, grant, identity
from test_portal_product import config as config_fixture
from test_portal_product import store as store_fixture

config = config_fixture
store = store_fixture


@pytest.mark.asyncio
async def test_real_hosted_model_run_persists_history_and_never_exposes_execute_tool(
    config, store, tmp_path, monkeypatch
):
    from citadel_agent.portal.product_contracts import TurnRequest

    requests = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["authorization"].startswith("Bearer ")
        assert body["model"] == config.model_deployment
        assert not any(tool["function"]["name"] == "execute_change" for tool in body.get("tools", []))
        assert any("Hosted portal boundary" in str(message.get("content")) for message in body["messages"])
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": int(NOW.timestamp()),
                "model": config.model_deployment,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Grounded test response"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
            },
        )

    original = openai.AsyncAzureOpenAI

    class MockAzureClient(original):
        def __init__(self, **kwargs):
            super().__init__(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs)

    monkeypatch.setattr(openai, "AsyncAzureOpenAI", MockAzureClient)
    runtime = SimpleNamespace(
        store=ChangeStore(tmp_path / "state" / "changes"),
        principal=SimpleNamespace(label="portal user"),
        caller=SimpleNamespace(persona=SimpleNamespace(value="consumer")),
        loaded=SimpleNamespace(error=None),
        policy=SimpleNamespace(
            default_environment="prod",
            environments={
                "prod": SimpleNamespace(environment_class="prod", mode=SimpleNamespace(value="pr"))
            },
        ),
        settings=SimpleNamespace(environment="prod"),
        project_root=Path(__file__).resolve().parents[1],
        docs=lambda: DocIndex([]),
        audit=SimpleNamespace(record=lambda *args, **kwargs: None),
    )

    class Factory:
        credential = SimpleNamespace(get_token=lambda _: AccessToken("test-model-token", 9999999999))

        @contextmanager
        def workspace(self):
            yield tmp_path, SimpleNamespace()

        def create(self, root, workspace, user, user_grant):
            return runtime

    factory = Factory()
    factory.config = config
    service = PortalAssistantService(store, factory)
    user = identity(roles=frozenset({PortalRole.VIEWER}))
    conversation = service.create_conversation(user)
    first = await service.turn(
        conversation.id, TurnRequest(request_id=uuid4(), message="Explain the gateway"), user, grant(config)
    )
    assert first.messages[-1].content == "Grounded test response"
    service = PortalAssistantService(store, factory)
    second = await service.turn(
        conversation.id, TurnRequest(request_id=uuid4(), message="Explain more"), user, grant(config)
    )
    assert len(second.messages) == 4
    assert len(requests) == 2
    assert any(message.get("content") == "Explain the gateway" for message in requests[1]["messages"])
    assert any(message.get("content") == "Grounded test response" for message in requests[1]["messages"])
