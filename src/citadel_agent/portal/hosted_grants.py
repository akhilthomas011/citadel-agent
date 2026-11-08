# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Deployment-owned bootstrap grants; Entra application roles remain separately required."""

from citadel_agent.portal.api import HubGrantReader
from citadel_agent.portal.contracts import HubScope
from citadel_agent.portal.grants import VersionedHubGrant
from citadel_agent.portal.product_contracts import PortalHubConfig


class DeploymentHubGrantReader:
    def __init__(self, durable: HubGrantReader, config: PortalHubConfig) -> None:
        self.durable = durable
        self.config = config

    def get(self, subject_id: str, scope: HubScope) -> VersionedHubGrant | None:
        if scope != self.config.scope:
            return None
        stored = self.durable.get(subject_id, scope)
        if stored is not None:
            return stored
        configured = next(
            (grant for grant in self.config.initial_grants if grant.subject_id == subject_id), None
        )
        return (
            VersionedHubGrant(grant=configured, etag="deployment-config") if configured is not None else None
        )
