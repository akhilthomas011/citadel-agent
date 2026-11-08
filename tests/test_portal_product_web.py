"""Real BFF relays enforce opaque sessions, CSRF, private service credentials and safe UI headers."""

from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from citadel_agent.portal.auth import EntraTokenSettings, EntraTokenValidator
from citadel_agent.portal.auth_web import SESSION_COOKIE, PortalAuthConfig
from citadel_agent.portal.bff import HttpxAgentJobStatusClient, create_bff_app
from test_portal_bff import (
    CLIENT_ID,
    SESSION_REFERENCE,
    SESSION_SECRET,
    TENANT_ID,
    FakeOAuthClient,
    FakeTokenProvider,
    MemorySessionStore,
)
from test_portal_product import NOW, query


def bff(http, tmp_path, *, store=None):
    client = HttpxAgentJobStatusClient(http, "https://agent.internal")
    (tmp_path / "index.html").write_text("<html><h1>Citadel portal</h1></html>")
    return create_bff_app(
        auth_config=PortalAuthConfig(
            origin="https://portal.example", callback_uri="https://portal.example/auth/callback"
        ),
        oauth_client=FakeOAuthClient(),
        delegated_token_validator=EntraTokenValidator(
            EntraTokenSettings(
                tenant_id=TENANT_ID, audience="api://portal", allowed_client_ids=frozenset({CLIENT_ID})
            ),
            signing_key_resolver=lambda _: "unused",
        ),
        session_store=store or MemorySessionStore(),
        agent_token_provider=FakeTokenProvider(),
        agent_client=client,
        product_client=client,
        static_directory=tmp_path,
        clock=lambda: NOW,
    )


HEADERS = {
    "Cookie": f"{SESSION_COOKIE}={SESSION_SECRET}",
    "Origin": "https://portal.example",
    "X-CSRF-Token": "c" * 43,
}


@pytest.mark.asyncio
async def test_real_bff_product_relay_rejects_csrf_and_identity_spoofing(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["X-Portal-Session-Ref"] == SESSION_REFERENCE
        assert request.headers["Authorization"].startswith("Bearer ")
        assert "x-user-id" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, json={"requests": 10})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with TestClient(bff(http, tmp_path), base_url="https://portal.example") as client:
            body = query().to_param()
            assert client.post("/api/finops/query", json=body).status_code == 401
            assert (
                client.post("/api/finops/query", json=body, headers={"Cookie": HEADERS["Cookie"]}).status_code
                == 403
            )
            forged = {**HEADERS, "Origin": "https://evil.example"}
            assert client.post("/api/finops/query", json=body, headers=forged).status_code == 403
            assert not requests
            response = client.post("/api/finops/query", json=body, headers={**HEADERS, "X-User-Id": "forged"})
            assert response.status_code == 200
            assert response.json() == {"requests": 10}
            assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/finops/reports"),
        ("GET", "/api/finops/reports"),
        ("GET", "/api/finops/reports/" + "a" * 64),
        ("POST", "/api/finops/reports/" + "a" * 64 + "/advance"),
        ("POST", "/api/finops/reports/" + "a" * 64 + "/cancel"),
        ("POST", "/api/finops/reports/" + "a" * 64 + "/export"),
    ],
)
@pytest.mark.asyncio
async def test_report_relays_recheck_session_csrf_and_service_identity(tmp_path, method, path):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == method
        assert request.url.path == path.replace("/api/", "/internal/", 1)
        assert request.headers["X-Portal-Session-Ref"] == SESSION_REFERENCE
        assert request.headers["Authorization"].startswith("Bearer ")
        assert request.headers["Authorization"] != "Bearer browser-spoof"
        assert "x-user-id" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, json={"id": "a" * 64})

    body = {"requestId": str(uuid4()), "query": query().to_param()} if path.endswith("/reports") else None
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with TestClient(bff(http, tmp_path), base_url="https://portal.example") as client:
            assert client.request(method, path, json=body).status_code == 401
            if method == "POST":
                assert (
                    client.request(method, path, json=body, headers={"Cookie": HEADERS["Cookie"]}).status_code
                    == 403
                )
                assert (
                    client.request(
                        method, path, json=body, headers={**HEADERS, "Origin": "https://evil.example"}
                    ).status_code
                    == 403
                )
            assert requests == []
            response = client.request(
                method,
                path,
                json=body,
                headers={**HEADERS, "X-User-Id": "forged", "Authorization": "Bearer browser-spoof"},
            )
            assert response.status_code == (202 if method == "POST" and path.endswith("/reports") else 200)
            assert response.headers["cache-control"] == "no-store"
            assert len(requests) == 1


@pytest.mark.parametrize("status", [400, 403, 404, 409, 422, 500])
@pytest.mark.asyncio
async def test_bff_product_errors_do_not_expose_internal_payloads(tmp_path, status):
    def handler(request):
        return httpx.Response(status, json={"detail": "secret source URL and diagnostic internals"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with TestClient(bff(http, tmp_path), base_url="https://portal.example") as client:
            response = client.get("/api/changes", headers=HEADERS)
    assert response.status_code == (503 if status == 500 else status)
    assert "secret source URL" not in response.text


@pytest.mark.asyncio
async def test_spa_is_same_origin_and_unknown_api_paths_never_return_html(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    ) as http:
        with TestClient(bff(http, tmp_path), base_url="https://portal.example") as client:
            for path in ("/", "/assistant", "/finops", "/operations"):
                response = client.get(path)
                assert response.status_code == 200
                assert response.headers["x-frame-options"] == "DENY"
                assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
                assert "Citadel portal" in response.text
            assert client.get("/api/unknown").status_code == 404
            assert client.get("/assets/../../.env").status_code == 404


@pytest.mark.asyncio
async def test_expired_browser_session_cannot_submit_a_turn(tmp_path):
    store = MemorySessionStore()
    store.session = store.session.model_copy(update={"expires_at": NOW - timedelta(minutes=1)})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("agent must not be called"))
    ) as http:
        with TestClient(bff(http, tmp_path, store=store), base_url="https://portal.example") as client:
            response = client.post("/api/conversations", json={}, headers=HEADERS)
            assert response.status_code == 401
