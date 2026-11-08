# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Durable per-user hub/environment grants separate from Entra application roles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from pydantic import Field

from citadel_agent.contracts.common import CitadelModel
from citadel_agent.portal.authz import HubGrant
from citadel_agent.portal.contracts import HubScope
from citadel_agent.portal.sessions import (
    CosmosContainer,
    PortalStateConflict,
    cosmos_model_fields,
)


class HubGrantDocument(CitadelModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["hub-grant"] = "hub-grant"
    grant: HubGrant


@dataclass(frozen=True)
class VersionedHubGrant:
    grant: HubGrant
    etag: str


class CosmosHubGrantStore:
    """Lookup and manage grants by deterministic key in the application-owned state container."""

    def __init__(self, container: CosmosContainer) -> None:
        self._container = container

    def get(self, subject_id: str, scope: HubScope) -> VersionedHubGrant | None:
        grant_id = _grant_id(subject_id, scope)
        try:
            document = self._container.read_item(item=grant_id, partition_key=grant_id)
        except CosmosResourceNotFoundError:
            return None
        etag = document.get("_etag")
        if not isinstance(etag, str):
            raise RuntimeError("Cosmos hub grant is missing its ETag.")
        record = HubGrantDocument.model_validate(cosmos_model_fields(document, HubGrantDocument))
        if record.grant.subject_id != subject_id or record.grant.scope != scope:
            raise RuntimeError("Cosmos hub grant key does not match its document.")
        return VersionedHubGrant(grant=record.grant, etag=etag)

    def assign(self, grant: HubGrant, *, expected_etag: str | None = None) -> VersionedHubGrant:
        grant_id = _grant_id(grant.subject_id, grant.scope)
        record = HubGrantDocument(id=grant_id, grant=grant)
        body = record.model_dump(by_alias=True, mode="json")
        if expected_etag is None:
            try:
                self._container.create_item(body=body)
            except CosmosResourceExistsError as exc:
                raise PortalStateConflict("Hub grant already exists; reload it before replacing.") from exc
        else:
            try:
                self._container.replace_item(
                    item=grant_id,
                    body=body,
                    partition_key=grant_id,
                    etag=expected_etag,
                    match_condition=MatchConditions.IfNotModified,
                )
            except (CosmosAccessConditionFailedError, CosmosResourceNotFoundError) as exc:
                raise PortalStateConflict("Hub grant changed; reload it before replacing.") from exc
        assigned = self.get(grant.subject_id, grant.scope)
        if assigned is None:
            raise RuntimeError("Cosmos did not persist the hub grant.")
        return assigned

    def revoke(self, subject_id: str, scope: HubScope, *, expected_etag: str) -> None:
        grant_id = _grant_id(subject_id, scope)
        try:
            self._container.delete_item(
                item=grant_id,
                partition_key=grant_id,
                etag=expected_etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (CosmosAccessConditionFailedError, CosmosResourceNotFoundError) as exc:
            raise PortalStateConflict("Hub grant changed or was already revoked.") from exc


def _grant_id(subject_id: str, scope: HubScope) -> str:
    payload = json.dumps(
        {
            "subjectId": subject_id.lower(),
            "tenantId": scope.tenant_id.lower(),
            "subscriptionId": scope.subscription_id.lower(),
            "hubId": scope.hub_id,
            "environment": scope.environment,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
