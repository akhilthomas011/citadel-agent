"""Hosted app construction uses the configured managed identity and existing Cosmos state."""

from types import SimpleNamespace

import pytest
from azure.core.exceptions import HttpResponseError
from fastapi.testclient import TestClient

from citadel_agent.portal.runtime import (
    PortalRuntimeSettings,
    create_agent_app_from_settings,
    create_bff_app_from_settings,
)


class FakeContainer:
    def __init__(self, *, fail_read: bool = False) -> None:
        self.read_count = 0
        self.fail_read = fail_read

    def read(self):
        self.read_count += 1
        if self.fail_read:
            raise HttpResponseError(message="Cosmos is unavailable.")
        return {"id": "portal-state"}


class FakeCosmosClient:
    def __init__(self, container: FakeContainer) -> None:
        self.container = container
        self.database_name = None
        self.container_name = None

    def get_database_client(self, name: str):
        self.database_name = name
        return self

    def get_container_client(self, name: str):
        self.container_name = name
        return self.container


class FakeCredential:
    def get_token(self, scope: str):
        return SimpleNamespace(token="credential-token")

    def close(self):
        pass


class FakeSecretReader:
    def get_secret_in_memory(self, name: str) -> str:
        assert name == "citadel-portal-client-secret"
        return "test-only-secret"


def _settings() -> PortalRuntimeSettings:
    return PortalRuntimeSettings(
        tenant_id="11111111-1111-4111-8111-111111111111",
        client_id="2d2d06d2-5c6e-4166-95ce-e5a63ca6441a",
        api_audience="api://citadel-portal",
        delegated_scope="api://citadel-portal/access_as_user",
        delegated_scope_name="access_as_user",
        client_secret_name="citadel-portal-client-secret",
        key_vault_url="https://citadel-kv.vault.azure.net/",
        cosmos_endpoint="https://citadel-cosmos.documents.azure.com:443/",
        cosmos_database="citadel-portal",
        cosmos_container="portal-state",
        portal_origin="https://portal.example",
        callback_uri="https://portal.example/auth/callback",
        agent_api_url="https://citadel-agent.internal",
        agent_api_audience="api://citadel-agent",
    )


def test_agent_app_from_settings_uses_existing_shared_cosmos_state() -> None:
    container = FakeContainer()
    cosmos = FakeCosmosClient(container)
    app = create_agent_app_from_settings(
        _settings(),
        credential=FakeCredential(),
        cosmos_client=cosmos,
    )

    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "live"}
        assert client.get("/health/ready").json() == {"status": "ready"}

    assert cosmos.database_name == "citadel-portal"
    assert cosmos.container_name == "portal-state"
    assert container.read_count == 1


def test_app_readiness_reports_cosmos_failure_without_leaking_details() -> None:
    container = FakeContainer(fail_read=True)
    app = create_agent_app_from_settings(
        _settings(),
        credential=FakeCredential(),
        cosmos_client=FakeCosmosClient(container),
    )

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Portal state store is unavailable."}


def test_bff_app_from_settings_loads_secret_and_checks_state_readiness() -> None:
    container = FakeContainer()
    app = create_bff_app_from_settings(
        _settings(),
        credential=FakeCredential(),
        cosmos_client=FakeCosmosClient(container),
        secret_reader=FakeSecretReader(),
    )

    with TestClient(app) as client:
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert client.get("/auth/session").json() == {"authenticated": False}


def test_hosted_runtime_rejects_insecure_endpoints() -> None:
    settings = _settings().model_dump()
    settings["agent_api_url"] = "http://citadel-agent.internal"

    with pytest.raises(ValueError, match="HTTPS"):
        PortalRuntimeSettings(**settings)
