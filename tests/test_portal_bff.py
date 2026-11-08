"""The browser BFF relays job status using managed identity and server-side sessions."""

import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from azure.core.credentials import AccessToken
from fastapi.testclient import TestClient

from citadel_agent.portal.api import PortalJobStatusResponse
from citadel_agent.portal.auth import EntraTokenSettings, EntraTokenValidator, PortalRole
from citadel_agent.portal.auth_web import SESSION_COOKIE, PortalAuthConfig
from citadel_agent.portal.bff import (
    AgentApiError,
    HttpxAgentJobStatusClient,
    ManagedIdentityAgentTokenProvider,
    create_bff_app,
)
from citadel_agent.portal.sessions import PortalSessionRecord

TENANT_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "e1992d1d-a8d2-48d4-b7a6-80e287879d88"
CLIENT_ID = "2d2d06d2-5c6e-4166-95ce-e5a63ca6441a"
NOW = datetime.now(UTC)
SESSION_SECRET = "s" * 43
SESSION_REFERENCE = hashlib.sha256(SESSION_SECRET.encode("ascii")).hexdigest()
JOB_STATUS = PortalJobStatusResponse(
    jobId="b" * 64,
    jobKind="assistant-turn",
    scope={
        "tenantId": TENANT_ID,
        "hubId": "citadel-production",
        "environment": "prod",
        "subscriptionId": "22222222-2222-4222-8222-222222222222",
    },
    status="running",
    createdAt=NOW - timedelta(seconds=15),
    updatedAt=NOW,
    startedAt=NOW - timedelta(seconds=10),
    completedAt=None,
    errorCode=None,
)


class MemorySessionStore:
    def __init__(self) -> None:
        self.session = PortalSessionRecord(
            id=SESSION_REFERENCE,
            tenant_id=TENANT_ID,
            subject_id=USER_ID,
            client_id=CLIENT_ID,
            roles=frozenset({PortalRole.VIEWER}),
            scopes=frozenset({"access_as_user"}),
            csrf_token="c" * 43,
            created_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=10),
            ttl=600,
        )

    def get_session(self, record_id, now):
        if record_id == self.session.id and now < self.session.expires_at:
            return self.session
        return None

    def create_auth_flow(self, record):
        raise NotImplementedError

    def consume_auth_flow(self, record_id, now):
        raise NotImplementedError

    def create_session(self, record):
        raise NotImplementedError

    def delete_session(self, record_id):
        raise NotImplementedError


class FakeOAuthClient:
    def begin(self):
        raise NotImplementedError

    def complete(self, flow, response):
        raise NotImplementedError


class FakeTokenProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get_token(self) -> str:
        self.calls += 1
        return "service-access-token"


class FakeAgentClient:
    def __init__(self, result=JOB_STATUS) -> None:
        self.result = result
        self.call = None

    async def get_job_status(self, *, token: str, session_reference: str, job_id: str):
        self.call = (token, session_reference, job_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _app(client: FakeAgentClient, token_provider: FakeTokenProvider):
    settings = EntraTokenSettings(
        tenant_id=TENANT_ID,
        audience="api://citadel-portal",
        allowed_client_ids=frozenset({CLIENT_ID}),
    )
    return create_bff_app(
        auth_config=PortalAuthConfig(
            origin="https://portal.example",
            callback_uri="https://portal.example/auth/callback",
        ),
        oauth_client=FakeOAuthClient(),
        delegated_token_validator=EntraTokenValidator(
            settings,
            signing_key_resolver=lambda _token: "unused",
        ),
        session_store=MemorySessionStore(),
        agent_token_provider=token_provider,
        agent_client=client,
        clock=lambda: NOW,
    )


def _request(client: TestClient, *, authenticated: bool = True):
    headers = {"Cookie": f"{SESSION_COOKIE}={SESSION_SECRET}"} if authenticated else {}
    return client.get(f"/api/jobs/{'b' * 64}", headers=headers)


def test_bff_rejects_missing_session_before_acquiring_agent_token() -> None:
    provider = FakeTokenProvider()
    client = TestClient(_app(FakeAgentClient(), provider), base_url="https://portal.example")

    response = _request(client, authenticated=False)

    assert response.status_code == 401
    assert provider.calls == 0


def test_bff_forwards_only_hashed_session_reference_with_service_token() -> None:
    provider = FakeTokenProvider()
    agent = FakeAgentClient()
    client = TestClient(_app(agent, provider), base_url="https://portal.example")

    response = _request(client)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["jobId"] == JOB_STATUS.job_id
    assert agent.call == ("service-access-token", SESSION_REFERENCE, "b" * 64)


@pytest.mark.parametrize(
    ("agent_status", "expected_status"),
    [(403, 403), (404, 404), (401, 503), (503, 503)],
)
def test_bff_maps_private_agent_failures_without_exposing_internal_details(
    agent_status,
    expected_status,
) -> None:
    provider = FakeTokenProvider()
    client = TestClient(_app(FakeAgentClient(AgentApiError(agent_status)), provider))

    response = _request(client)

    assert response.status_code == expected_status
    assert response.json()["detail"] in {
        "Job is unavailable.",
        "Private agent service is unavailable.",
        "Private agent service authentication failed.",
    }


def test_managed_identity_token_provider_requests_agent_default_scope() -> None:
    class Credential:
        def get_token(self, scope: str):
            self.scope = scope
            return AccessToken("managed-identity-token", int((NOW + timedelta(minutes=5)).timestamp()))

    credential = Credential()
    provider = ManagedIdentityAgentTokenProvider(credential, "api://citadel-agent/")

    assert provider.get_token() == "managed-identity-token"
    assert credential.scope == "api://citadel-agent/.default"


def test_managed_identity_scope_can_differ_from_v2_token_audience() -> None:
    scopes = []

    class Credential:
        def get_token(self, scope: str):
            scopes.append(scope)
            return AccessToken("service-token", 9999999999)

    provider = ManagedIdentityAgentTokenProvider(
        Credential(), CLIENT_ID, token_scope=f"api://{CLIENT_ID}/.default"
    )
    assert provider.get_token() == "service-token"
    assert scopes == [f"api://{CLIENT_ID}/.default"]
    with pytest.raises(ValueError, match="single application"):
        ManagedIdentityAgentTokenProvider(Credential(), CLIENT_ID, token_scope="api://other/access_as_user")


@pytest.mark.asyncio
async def test_http_agent_client_sends_service_and_hashed_session_headers() -> None:
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        captured["url"] = str(request.url)
        return httpx.Response(200, json=JOB_STATUS.model_dump(by_alias=True, mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        agent = HttpxAgentJobStatusClient(http, "https://agent.internal")
        result = await agent.get_job_status(
            token="app-token",
            session_reference=SESSION_REFERENCE,
            job_id="b" * 64,
        )

    assert result == JOB_STATUS
    assert captured["authorization"] == "Bearer app-token"
    assert captured["x-portal-session-ref"] == SESSION_REFERENCE
    assert captured["url"] == f"https://agent.internal/internal/jobs/{'b' * 64}"


def test_http_agent_client_rejects_non_https_endpoint() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        HttpxAgentJobStatusClient(httpx.AsyncClient(), "http://agent.internal")
