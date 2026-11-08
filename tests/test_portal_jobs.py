"""Portal jobs are durable, idempotent and updated with compare-and-swap ETags."""

from datetime import UTC, datetime, timedelta

import pytest
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from citadel_agent.portal.contracts import HubScope
from citadel_agent.portal.jobs import (
    CosmosPortalJobStore,
    PortalJobKind,
    PortalJobStatus,
)
from citadel_agent.portal.sessions import PortalStateConflict

TENANT_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "e1992d1d-a8d2-48d4-b7a6-80e287879d88"
NOW = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)
IDEMPOTENCY = "idempotency-key-001"


class FakeContainer:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, object]] = {}
        self.etag_number = 0

    def create_item(self, body):
        record_id = body["id"]
        if record_id in self.items:
            raise CosmosResourceExistsError(status_code=409)
        self.etag_number += 1
        self.items[record_id] = {**body, "_etag": f"etag-{self.etag_number}"}

    def read_item(self, item, partition_key):
        assert item == partition_key
        if item not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        return self.items[item]

    def delete_item(self, item, partition_key, *, etag=None, match_condition=None):
        assert item == partition_key
        if item not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        if etag is not None and self.items[item]["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412)
        del self.items[item]

    def replace_item(
        self,
        item,
        body,
        partition_key,
        *,
        etag=None,
        match_condition=None,
    ):
        assert item == partition_key
        if item not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        if etag is not None and self.items[item]["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412)
        self.etag_number += 1
        self.items[item] = {**body, "_etag": f"etag-{self.etag_number}"}


def _scope() -> HubScope:
    return HubScope(
        tenant_id=TENANT_ID,
        hub_id="production-hub",
        environment="prod",
        subscription_id="22222222-2222-4222-8222-222222222222",
    )


def _submit(store: CosmosPortalJobStore, *, request_hash: str | None = None):
    return store.submit(
        job_kind=PortalJobKind.CHANGE_PREVIEW,
        scope=_scope(),
        subject_id=USER_ID,
        idempotency_key=IDEMPOTENCY,
        request_hash=request_hash or "a" * 64,
        now=NOW,
        ttl_seconds=86400,
    )


def test_retries_with_same_key_and_payload_return_the_original_job() -> None:
    container = FakeContainer()
    store = CosmosPortalJobStore(container)

    first = _submit(store)
    retry = _submit(store)

    assert first.created is True
    assert retry.created is False
    assert retry.record.id == first.record.id
    assert retry.etag == first.etag
    assert len(container.items) == 1


def test_same_idempotency_key_with_different_payload_is_rejected() -> None:
    store = CosmosPortalJobStore(FakeContainer())
    _submit(store)

    with pytest.raises(PortalStateConflict, match="different job request"):
        _submit(store, request_hash="b" * 64)


def test_job_transitions_are_monotonic_and_etag_guarded() -> None:
    store = CosmosPortalJobStore(FakeContainer())
    queued = _submit(store)
    running = store.transition(
        queued.record.id,
        expected_etag=queued.etag,
        status=PortalJobStatus.RUNNING,
        now=NOW + timedelta(seconds=1),
    )
    assert running.record.started_at == NOW + timedelta(seconds=1)

    with pytest.raises(PortalStateConflict, match="changed"):
        store.transition(
            queued.record.id,
            expected_etag=queued.etag,
            status=PortalJobStatus.CANCELLED,
            now=NOW + timedelta(seconds=2),
        )

    succeeded = store.transition(
        queued.record.id,
        expected_etag=running.etag,
        status=PortalJobStatus.SUCCEEDED,
        now=NOW + timedelta(seconds=3),
        result_refs={"changeId": "cs-20261016-060000-123abc"},
    )
    assert succeeded.record.status is PortalJobStatus.SUCCEEDED
    assert succeeded.record.completed_at == NOW + timedelta(seconds=3)
    with pytest.raises(PortalStateConflict, match="Invalid portal job transition"):
        store.transition(
            queued.record.id,
            expected_etag=succeeded.etag,
            status=PortalJobStatus.RUNNING,
            now=NOW + timedelta(seconds=4),
        )


def test_job_idempotency_is_scoped_by_user_hub_and_kind() -> None:
    store = CosmosPortalJobStore(FakeContainer())
    original = _submit(store)
    other_user = store.submit(
        job_kind=PortalJobKind.CHANGE_PREVIEW,
        scope=_scope(),
        subject_id="f2c6a8db-6a47-4c0e-851d-1636bdf4b023",
        idempotency_key=IDEMPOTENCY,
        request_hash="a" * 64,
        now=NOW,
        ttl_seconds=86400,
    )
    other_job_kind = store.submit(
        job_kind=PortalJobKind.ANALYTICS_EXPORT,
        scope=_scope(),
        subject_id=USER_ID,
        idempotency_key=IDEMPOTENCY,
        request_hash="a" * 64,
        now=NOW,
        ttl_seconds=86400,
    )
    assert len({original.record.id, other_user.record.id, other_job_kind.record.id}) == 3
