"""Hub and environment grants are persisted and changed with ETag preconditions."""

import pytest
from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from citadel_agent.portal.authz import HubGrant, PortalGrantPermission
from citadel_agent.portal.contracts import HubScope
from citadel_agent.portal.grants import CosmosHubGrantStore
from citadel_agent.portal.sessions import PortalStateConflict

TENANT_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "e1992d1d-a8d2-48d4-b7a6-80e287879d88"


class FakeContainer:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, object]] = {}
        self.etag_number = 0

    def create_item(self, body):
        if body["id"] in self.items:
            raise CosmosResourceExistsError(status_code=409)
        self.etag_number += 1
        self.items[body["id"]] = {**body, "_etag": f"etag-{self.etag_number}"}

    def read_item(self, item, partition_key):
        assert item == partition_key
        if item not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        return self.items[item]

    def replace_item(self, item, body, partition_key, *, etag=None, match_condition=None):
        assert item == partition_key
        if item not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        if self.items[item]["_etag"] != etag or match_condition is not MatchConditions.IfNotModified:
            raise CosmosAccessConditionFailedError(status_code=412)
        self.etag_number += 1
        self.items[item] = {**body, "_etag": f"etag-{self.etag_number}"}

    def delete_item(self, item, partition_key, *, etag=None, match_condition=None):
        assert item == partition_key
        if item not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        if self.items[item]["_etag"] != etag or match_condition is not MatchConditions.IfNotModified:
            raise CosmosAccessConditionFailedError(status_code=412)
        del self.items[item]


def _scope(environment: str = "prod") -> HubScope:
    return HubScope(
        tenant_id=TENANT_ID,
        hub_id="citadel-production",
        environment=environment,
        subscription_id="22222222-2222-4222-8222-222222222222",
    )


def _grant(scope: HubScope, permissions=frozenset()) -> HubGrant:
    return HubGrant(subject_id=USER_ID, scope=scope, permissions=permissions)


def test_grants_are_scoped_by_user_hub_and_environment() -> None:
    store = CosmosHubGrantStore(FakeContainer())
    scope = _scope()
    grant = _grant(scope, frozenset({PortalGrantPermission.FINOPS_DATA}))
    stored = store.assign(grant)

    assert store.get(USER_ID, scope) == stored
    assert store.get(USER_ID, _scope("staging")) is None
    assert store.get("10c669b5-15ea-48b0-9e11-232a29be4d74", scope) is None


def test_grant_update_and_revocation_require_current_etag() -> None:
    store = CosmosHubGrantStore(FakeContainer())
    scope = _scope()
    initial = store.assign(_grant(scope))
    updated = store.assign(
        _grant(scope, frozenset({PortalGrantPermission.RAW_LOGS})),
        expected_etag=initial.etag,
    )
    assert updated.etag != initial.etag
    assert updated.grant.permissions == frozenset({PortalGrantPermission.RAW_LOGS})

    with pytest.raises(PortalStateConflict, match="changed"):
        store.assign(_grant(scope), expected_etag=initial.etag)

    store.revoke(USER_ID, scope, expected_etag=updated.etag)
    assert store.get(USER_ID, scope) is None
    with pytest.raises(PortalStateConflict, match="revoked"):
        store.revoke(USER_ID, scope, expected_etag=updated.etag)


def test_new_assignment_cannot_overwrite_existing_grant() -> None:
    store = CosmosHubGrantStore(FakeContainer())
    scope = _scope()
    store.assign(_grant(scope))

    with pytest.raises(PortalStateConflict, match="already exists"):
        store.assign(_grant(scope, frozenset({PortalGrantPermission.EXPORTS})))
