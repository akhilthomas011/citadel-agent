# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Resolved Azure coordinates of a Citadel environment (supplied by the admin policy)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from citadel_agent.contracts.common import AzureResourceRef, CitadelModel, GuidStr, HttpsUrl

EnvironmentClass = Literal["dev", "test", "prod"]


class ManagedIdentityRef(CitadelModel):
    """The APIM user-assigned managed identity (used for backend and agent authentication)."""

    subscription_id: GuidStr
    resource_group_name: str = Field(min_length=1)
    name: str = Field(min_length=1)
    client_id: GuidStr | None = None


class EnvironmentTarget(CitadelModel):
    """Everything the renderer needs to know about the environment a contract is deployed to."""

    name: str = Field(min_length=1)
    environment_class: EnvironmentClass
    subscription_id: GuidStr
    location: str = Field(min_length=1)
    tenant_id: GuidStr | None = None
    apim: AzureResourceRef
    gateway_url: HttpsUrl | None = None
    apim_managed_identity: ManagedIdentityRef | None = None
    key_vault: AzureResourceRef | None = None
    app_insights: AzureResourceRef | None = None
