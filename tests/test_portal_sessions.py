"""Cosmos portal sessions expire and one-time auth state cannot be replayed."""

from datetime import UTC, datetime, timedelta

import pytest
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from citadel_agent.portal.auth import PortalRole
from citadel_agent.portal.sessions import (
    AuthFlowRecord,
    CosmosPortalSessionStore,
    PortalSessionRecord,
    PortalStateConflict,
)

NOW = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)
SESSION_ID = "a" * 64
FLOW_ID = "b" * 64


class FakeContainer:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, object]] = {}
        self.fail_next_conditional_delete = False

    def create_item(self, body):
        record_id = body["id"]
        if record_id in self.items:
            raise CosmosResourceExistsError(status_code=409)
        self.items[record_id] = {**body, "_etag": "etag-1"}

    def read_item(self, item, partition_key):
        assert item == partition_key
        if item not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        return self.items[item]

    def delete_item(self, item, partition_key, *, etag=None, match_condition=None):
        assert item == partition_key
        if item not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        if etag is not None and self.fail_next_conditional_delete:
            self.fail_next_conditional_delete = False
            raise CosmosAccessConditionFailedError(status_code=412)
        if etag is not None and self.items[item]["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412)
        del self.items[item]


def _session(
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
) -> PortalSessionRecord:
    return PortalSessionRecord(
        id=SESSION_ID,
        tenant_id="11111111-1111-4111-8111-111111111111",
        subject_id="e1992d1d-a8d2-48d4-b7a6-80e287879d88",
        client_id="2d2d06d2-5c6e-4166-95ce-e5a63ca6441a",
        roles=frozenset({PortalRole.VIEWER}),
        scopes=frozenset({"access_as_user"}),
        csrf_token="c" * 43,
        created_at=created_at or NOW,
        expires_at=expires_at or NOW + timedelta(minutes=15),
        ttl=900,
    )


def test_cosmos_store_creates_and_reads_a_session_without_system_fields() -> None:
    container = FakeContainer()
    store = CosmosPortalSessionStore(container)
    store.create_session(_session())

    session = store.get_session(SESSION_ID, NOW)
    assert session == _session()
    assert container.items[SESSION_ID]["ttl"] == 900


def test_expired_session_is_deleted_and_not_returned() -> None:
    container = FakeContainer()
    store = CosmosPortalSessionStore(container)
    store.create_session(
        _session(
            created_at=NOW - timedelta(minutes=5),
            expires_at=NOW - timedelta(seconds=1),
        )
    )

    assert store.get_session(SESSION_ID, NOW) is None
    assert SESSION_ID not in container.items


def test_auth_flow_is_consumed_once_with_etag_protection() -> None:
    container = FakeContainer()
    store = CosmosPortalSessionStore(container)
    flow = AuthFlowRecord(
        id=FLOW_ID,
        flow={"state": "random-state", "code_verifier": "one-time"},
        expires_at=NOW + timedelta(minutes=5),
        ttl=300,
    )
    store.create_auth_flow(flow)

    assert store.consume_auth_flow(FLOW_ID, NOW) == flow
    assert store.consume_auth_flow(FLOW_ID, NOW) is None


def test_replayed_flow_loses_etag_race() -> None:
    container = FakeContainer()
    store = CosmosPortalSessionStore(container)
    store.create_auth_flow(
        AuthFlowRecord(
            id=FLOW_ID,
            flow={"state": "random-state"},
            expires_at=NOW + timedelta(minutes=5),
            ttl=300,
        )
    )
    container.fail_next_conditional_delete = True

    assert store.consume_auth_flow(FLOW_ID, NOW) is None
    assert FLOW_ID in container.items


def test_duplicate_session_id_is_an_explicit_conflict() -> None:
    store = CosmosPortalSessionStore(FakeContainer())
    store.create_session(_session())

    with pytest.raises(PortalStateConflict):
        store.create_session(_session())


def test_store_rejects_naive_current_time() -> None:
    store = CosmosPortalSessionStore(FakeContainer())
    with pytest.raises(ValueError, match="timezone"):
        store.get_session(SESSION_ID, datetime(2026, 10, 16, 6))
