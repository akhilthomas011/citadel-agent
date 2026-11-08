"""Portal application approvals bind reviewers to exact, current production evidence."""

from datetime import UTC, datetime, timedelta

import pytest
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from citadel_agent.portal.approvals import (
    ApprovalEvidence,
    ApprovalStatus,
    PortalApprovalService,
)
from citadel_agent.portal.auth import PortalIdentity
from citadel_agent.portal.authz import (
    HubGrant,
    PortalAuthorizationError,
)
from citadel_agent.portal.contracts import HubScope, PortalRole
from citadel_agent.portal.sessions import PortalStateConflict

TENANT_ID = "11111111-1111-4111-8111-111111111111"
AUTHOR_ID = "e1992d1d-a8d2-48d4-b7a6-80e287879d88"
APPROVER_ID = "10c669b5-15ea-48b0-9e11-232a29be4d74"
NOW = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)
CHANGE_ID = "cs-20261016-060000-123abc"


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

    def replace_item(self, item, body, partition_key, *, etag=None, match_condition=None):
        assert item == partition_key
        if item not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        if etag is not None and self.items[item]["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412)
        self.etag_number += 1
        self.items[item] = {**body, "_etag": f"etag-{self.etag_number}"}

    def delete_item(self, item, partition_key, *, etag=None, match_condition=None):
        raise NotImplementedError


def _scope(environment: str = "prod") -> HubScope:
    return HubScope(
        tenant_id=TENANT_ID,
        hub_id="production-hub",
        environment=environment,
        subscription_id="22222222-2222-4222-8222-222222222222",
    )


def _evidence(**overrides) -> ApprovalEvidence:
    values = {
        "change_hash": "a" * 64,
        "repository_base_commit": "b" * 40,
        "admin_policy_hash": "c" * 40,
        "upstream_commit": "d" * 40,
        "preview_hash": "e" * 64,
    }
    values.update(overrides)
    return ApprovalEvidence(**values)


def _identity(
    subject_id: str = APPROVER_ID,
    role: PortalRole = PortalRole.APPROVER,
) -> PortalIdentity:
    return PortalIdentity(
        subject_id=subject_id,
        tenant_id=TENANT_ID,
        client_id="2d2d06d2-5c6e-4166-95ce-e5a63ca6441a",
        expires_at=NOW + timedelta(minutes=15),
        scopes=frozenset({"access_as_user"}),
        roles=frozenset({role}),
    )


def _grant(scope: HubScope, subject_id: str = APPROVER_ID) -> HubGrant:
    return HubGrant(subject_id=subject_id, scope=scope)


def _create(service: PortalApprovalService, *, scope: HubScope | None = None, **kwargs):
    return service.create(
        change_id=CHANGE_ID,
        scope=scope or _scope(),
        author_id=AUTHOR_ID,
        evidence=kwargs.pop("evidence", _evidence()),
        now=NOW,
        ttl_seconds=300,
        **kwargs,
    )


def test_approval_requires_a_different_approver_and_exact_live_evidence() -> None:
    scope = _scope()
    service = PortalApprovalService(FakeContainer())
    pending = _create(service)

    approved = service.approve(
        pending.record.id,
        identity=_identity(),
        grant=_grant(scope),
        requested_scope=scope,
        current_evidence=_evidence(),
        now=NOW + timedelta(seconds=1),
    )

    assert approved.record.status is ApprovalStatus.APPROVED
    assert approved.record.approved_by == APPROVER_ID
    assert approved.record.approved_at == NOW + timedelta(seconds=1)
    assert approved.etag != pending.etag


def test_production_author_cannot_approve_own_change() -> None:
    scope = _scope()
    service = PortalApprovalService(FakeContainer())
    pending = _create(service)
    author_identity = _identity(subject_id=AUTHOR_ID)

    with pytest.raises(PortalAuthorizationError, match="independent approver"):
        service.approve(
            pending.record.id,
            identity=author_identity,
            grant=_grant(scope, AUTHOR_ID),
            requested_scope=scope,
            current_evidence=_evidence(),
            now=NOW + timedelta(seconds=1),
        )
    assert service.get(pending.record.id).record.status is ApprovalStatus.PENDING


def test_changed_evidence_invalidates_approval_and_cannot_be_approved() -> None:
    scope = _scope()
    service = PortalApprovalService(FakeContainer())
    pending = _create(service)

    with pytest.raises(PortalStateConflict, match="evidence changed"):
        service.approve(
            pending.record.id,
            identity=_identity(),
            grant=_grant(scope),
            requested_scope=scope,
            current_evidence=_evidence(preview_hash="f" * 64),
            now=NOW + timedelta(seconds=1),
        )
    invalidated = service.get(pending.record.id)
    assert invalidated.record.status is ApprovalStatus.INVALIDATED
    assert invalidated.record.invalidation_code == "evidence-changed"


def test_expired_approval_is_persistently_closed() -> None:
    service = PortalApprovalService(FakeContainer())
    pending = service.create(
        change_id=CHANGE_ID,
        scope=_scope(),
        author_id=AUTHOR_ID,
        evidence=_evidence(),
        now=NOW,
        ttl_seconds=60,
    )

    with pytest.raises(PortalStateConflict, match="expired"):
        service.approve(
            pending.record.id,
            identity=_identity(),
            grant=_grant(_scope()),
            requested_scope=_scope(),
            current_evidence=_evidence(),
            now=NOW + timedelta(seconds=61),
        )
    assert service.get(pending.record.id).record.status is ApprovalStatus.EXPIRED


def test_finops_or_viewer_role_cannot_approve_a_change() -> None:
    scope = _scope()
    service = PortalApprovalService(FakeContainer())
    pending = _create(service)
    viewer = _identity(role=PortalRole.VIEWER)

    with pytest.raises(PortalAuthorizationError):
        service.approve(
            pending.record.id,
            identity=viewer,
            grant=_grant(scope),
            requested_scope=scope,
            current_evidence=_evidence(),
            now=NOW + timedelta(seconds=1),
        )


def test_production_approval_requires_a_pinned_admin_policy() -> None:
    service = PortalApprovalService(FakeContainer())

    with pytest.raises(ValueError, match="pinned admin policy"):
        _create(service, evidence=_evidence(admin_policy_hash=None))


def test_approval_identity_isolated_by_hub_scope() -> None:
    service = PortalApprovalService(FakeContainer())
    first = _create(service, scope=_scope())
    second = _create(service, scope=_scope("staging"))
    assert first.record.id != second.record.id
