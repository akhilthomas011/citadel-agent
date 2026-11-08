"""Agent API callers require both the BFF app role and a live server-side user session."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from citadel_agent.portal.agent_auth import (
    AgentCallContext,
    create_agent_context_dependency,
)
from citadel_agent.portal.auth import EntraServiceTokenValidator, EntraTokenSettings, PortalRole
from citadel_agent.portal.sessions import PortalSessionRecord

TENANT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_TENANT_ID = "d94efdb2-f940-461a-b364-0fa281cd3af8"
BFF_CLIENT_ID = "2d2d06d2-5c6e-4166-95ce-e5a63ca6441a"
BFF_PRINCIPAL_ID = "0a563df8-9c61-4d73-90aa-36c103931fe9"
SESSION_ID = "a" * 64
NOW = datetime.now(UTC)


class MemorySessionStore:
    def __init__(self, session: PortalSessionRecord) -> None:
        self.session = session

    def get_session(self, record_id, now):
        if record_id != self.session.id or now >= self.session.expires_at:
            return None
        return self.session

    def create_auth_flow(self, record):
        raise NotImplementedError

    def consume_auth_flow(self, record_id, now):
        raise NotImplementedError

    def create_session(self, record):
        raise NotImplementedError

    def delete_session(self, record_id):
        raise NotImplementedError


@pytest.fixture
def service_tools():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = EntraTokenSettings(
        tenant_id=TENANT_ID,
        audience="api://citadel-agent",
        allowed_client_ids=frozenset({BFF_CLIENT_ID}),
    )
    validator = EntraServiceTokenValidator(
        settings,
        signing_key_resolver=lambda _token: private_key.public_key(),
    )
    return validator, private_key, settings


def _service_token(private_key, settings, **overrides) -> str:
    claims = {
        "aud": settings.audience,
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "iat": datetime.now(UTC),
        "iss": settings.issuer,
        "oid": BFF_PRINCIPAL_ID,
        "roles": ["Portal.InvokeAgent"],
        "tid": settings.tenant_id,
        "azp": BFF_CLIENT_ID,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def _session() -> PortalSessionRecord:
    return PortalSessionRecord(
        id=SESSION_ID,
        tenant_id=TENANT_ID,
        subject_id="e1992d1d-a8d2-48d4-b7a6-80e287879d88",
        client_id=BFF_CLIENT_ID,
        roles=frozenset({PortalRole.VIEWER}),
        scopes=frozenset({"access_as_user"}),
        csrf_token="c" * 43,
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
        ttl=600,
    )


def _client(service_tools, *, token_overrides=None):
    validator, private_key, settings = service_tools
    session = _session()
    token = _service_token(private_key, settings, **(token_overrides or {}))
    dependency = create_agent_context_dependency(
        service_token_validator=validator,
        session_store=MemorySessionStore(session),
        clock=lambda: NOW,
    )
    app = FastAPI()

    @app.get("/internal/test")
    def test_endpoint(context: Annotated[AgentCallContext, Depends(dependency)]):
        return {
            "userId": context.user.subject_id,
            "roles": sorted(role.value for role in context.user.roles),
            "serviceClientId": context.service.client_id,
        }

    return TestClient(app), token, session


def test_service_identity_resolves_user_only_from_shared_session(service_tools) -> None:
    client, token, session = _client(service_tools)
    response = client.get(
        "/internal/test",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Portal-Session-Ref": session.id,
            "X-Portal-Subject": "attacker-selected-user",
            "X-Portal-Roles": "Platform.Operator",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "userId": session.subject_id,
        "roles": [PortalRole.VIEWER.value],
        "serviceClientId": BFF_CLIENT_ID,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"azp": "496be3c8-0fbd-4a58-bf05-bd57256c29e4"},
        {"roles": ["Other.Role"]},
        {"scp": "access_as_user"},
        {"tid": OTHER_TENANT_ID},
    ],
)
def test_service_token_must_be_app_only_from_the_approved_client(
    service_tools,
    overrides,
) -> None:
    client, token, session = _client(service_tools, token_overrides=overrides)
    response = client.get(
        "/internal/test",
        headers={"Authorization": f"Bearer {token}", "X-Portal-Session-Ref": session.id},
    )
    assert response.status_code == 401


def test_agent_route_requires_a_valid_session_reference_and_service_token(service_tools) -> None:
    client, token, session = _client(service_tools)

    missing_header = client.get("/internal/test", headers={"Authorization": f"Bearer {token}"})
    invalid_reference = client.get(
        "/internal/test",
        headers={"Authorization": f"Bearer {token}", "X-Portal-Session-Ref": "not-a-reference"},
    )
    invalid_token = client.get(
        "/internal/test",
        headers={"Authorization": "Bearer invalid", "X-Portal-Session-Ref": session.id},
    )

    assert missing_header.status_code == 401
    assert invalid_reference.status_code == 401
    assert invalid_token.status_code == 401
