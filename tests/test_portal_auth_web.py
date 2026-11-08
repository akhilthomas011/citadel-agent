"""BFF sign-in uses one-time Entra state and secure server-side browser sessions."""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from citadel_agent.portal.auth import EntraTokenSettings, EntraTokenValidator, PortalRole
from citadel_agent.portal.auth_web import (
    AUTH_TRANSACTION_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    PortalAuthConfig,
    PortalOAuthError,
    create_auth_router,
)
from citadel_agent.portal.sessions import AuthFlowRecord, PortalSessionRecord

TENANT_ID = "11111111-1111-4111-8111-111111111111"
CLIENT_ID = "2d2d06d2-5c6e-4166-95ce-e5a63ca6441a"
USER_ID = "e1992d1d-a8d2-48d4-b7a6-80e287879d88"
ORIGIN = "https://portal.example"


class MemoryStore:
    def __init__(self) -> None:
        self.flows: dict[str, AuthFlowRecord] = {}
        self.sessions: dict[str, PortalSessionRecord] = {}

    def create_auth_flow(self, record: AuthFlowRecord) -> None:
        self.flows[record.id] = record

    def consume_auth_flow(self, record_id: str, now: datetime) -> AuthFlowRecord | None:
        record = self.flows.pop(record_id, None)
        if record is None or record.expires_at <= now:
            return None
        return record

    def create_session(self, record: PortalSessionRecord) -> None:
        self.sessions[record.id] = record

    def get_session(self, record_id: str, now: datetime) -> PortalSessionRecord | None:
        record = self.sessions.get(record_id)
        if record is None or record.expires_at <= now:
            return None
        return record

    def delete_session(self, record_id: str) -> None:
        self.sessions.pop(record_id, None)


class FakeOAuthClient:
    def __init__(self, access_token: str, authority: str) -> None:
        self.access_token = access_token
        self.authority = authority
        self.state = "test-state"

    def begin(self):
        return {
            "auth_uri": f"{self.authority}/oauth2/v2.0/authorize?state={self.state}",
            "state": self.state,
        }

    def complete(self, flow, response):
        if flow["state"] != response.get("state"):
            raise PortalOAuthError("state mismatch")
        return self.access_token


def _test_client(*, authority: str | None = None):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    token_settings = EntraTokenSettings(
        tenant_id=TENANT_ID,
        audience="api://citadel-portal",
        allowed_client_ids=frozenset({CLIENT_ID}),
    )
    token = jwt.encode(
        {
            "aud": token_settings.audience,
            "exp": now + timedelta(minutes=30),
            "iat": now,
            "iss": token_settings.issuer,
            "oid": USER_ID,
            "scp": "access_as_user",
            "tid": TENANT_ID,
            "azp": CLIENT_ID,
            "roles": [PortalRole.OPERATOR.value],
        },
        private_key,
        algorithm="RS256",
    )
    validator = EntraTokenValidator(
        token_settings,
        signing_key_resolver=lambda _token: private_key.public_key(),
    )
    store = MemoryStore()
    oauth = FakeOAuthClient(
        token,
        authority or f"https://login.microsoftonline.com/{TENANT_ID}",
    )
    config = PortalAuthConfig(
        origin=ORIGIN,
        callback_uri=f"{ORIGIN}/auth/callback",
    )
    app = FastAPI()
    app.include_router(
        create_auth_router(
            config=config,
            oauth_client=oauth,
            token_validator=validator,
            store=store,
            clock=lambda: now,
        )
    )
    return TestClient(app, base_url=ORIGIN), store, oauth


def _callback(client: TestClient, state: str = "test-state"):
    return client.post(
        "/auth/callback",
        content=urlencode({"code": "one-time-code", "state": state}),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def test_login_callback_and_session_never_expose_access_token() -> None:
    client, store, _ = _test_client()
    login = client.get("/auth/login", follow_redirects=False)

    assert login.status_code == 303
    assert login.headers["location"].startswith(
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize"
    )
    assert AUTH_TRANSACTION_COOKIE in login.headers["set-cookie"]
    assert "httponly" in login.headers["set-cookie"].lower()
    assert "samesite=none" in login.headers["set-cookie"].lower()

    callback = _callback(client)
    assert callback.status_code == 303
    assert callback.headers["location"] == "/"
    assert SESSION_COOKIE in callback.headers["set-cookie"]
    assert "httponly" in callback.headers["set-cookie"].lower()
    assert "secure" in callback.headers["set-cookie"].lower()
    assert len(store.sessions) == 1

    session = client.get("/auth/session")
    payload = session.json()
    assert session.headers["cache-control"] == "no-store"
    assert payload["authenticated"] is True
    assert payload["user"]["subjectId"] == USER_ID
    assert payload["user"]["roles"] == [PortalRole.OPERATOR.value]
    assert "access_token" not in session.text
    assert "one-time-code" not in session.text


def test_logout_requires_same_origin_and_session_csrf_token() -> None:
    client, store, _ = _test_client()
    client.get("/auth/login", follow_redirects=False)
    assert _callback(client).status_code == 303
    csrf_token = client.get("/auth/session").json()["csrfToken"]

    forbidden = client.post(
        "/auth/logout",
        headers={"Origin": "https://attacker.example", CSRF_HEADER: csrf_token},
    )
    assert forbidden.status_code == 403
    assert len(store.sessions) == 1

    logout = client.post(
        "/auth/logout",
        headers={"Origin": ORIGIN, CSRF_HEADER: csrf_token},
    )
    assert logout.status_code == 204
    assert not store.sessions
    assert client.get("/auth/session").json() == {"authenticated": False}


def test_invalid_state_and_replayed_callback_are_denied() -> None:
    client, store, _ = _test_client()
    client.get("/auth/login", follow_redirects=False)

    invalid = _callback(client, state="attacker-state")
    assert invalid.status_code == 401
    assert not store.flows
    replay = _callback(client)
    assert replay.status_code == 400


def test_login_rejects_untrusted_authorization_host() -> None:
    client, store, _ = _test_client(authority="https://attacker.example")

    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 503
    assert not store.flows


def test_callback_requires_transaction_cookie_and_form_encoded_body() -> None:
    client, _, _ = _test_client()
    missing_transaction = _callback(client)
    assert missing_transaction.status_code == 400

    client.get("/auth/login", follow_redirects=False)
    invalid_content_type = client.post(
        "/auth/callback",
        content=urlencode({"code": "code", "state": "test-state"}),
        headers={"content-type": "application/json"},
    )
    assert invalid_content_type.status_code == 400


def test_duplicate_oauth_parameters_are_rejected() -> None:
    client, _, _ = _test_client()
    client.get("/auth/login", follow_redirects=False)
    response = client.post(
        "/auth/callback",
        content="code=first&code=second&state=test-state",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400
