# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Finite private analytics worker, with no browser session, model or repository credentials."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError
from azure.cosmos import CosmosClient
from azure.identity import ManagedIdentityCredential
from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from citadel_agent.analytics.materialize import AnalyticsMaterializer
from citadel_agent.analytics.snapshots import AnalyticsConflict, AnalyticsStore, AnalyticsUnavailable
from citadel_agent.azure_clients.cosmos import CosmosUsageReader
from citadel_agent.portal.data import PortalDataService
from citadel_agent.portal.product_contracts import PortalHubConfig

logger = logging.getLogger(__name__)


class AnalyticsWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CITADEL_PORTAL_", env_file=".env", extra="ignore")

    cosmos_endpoint: str
    cosmos_database: str = Field(min_length=1, max_length=128)
    analytics_container: str = Field(min_length=1, max_length=128)
    hub_config: PortalHubConfig
    managed_identity_client_id: str | None = None
    analytics_retention_days: int = Field(default=93, ge=1, le=366)
    analytics_max_work_seconds: int = Field(default=240, ge=1, le=1500)

    @field_validator("managed_identity_client_id")
    @classmethod
    def _identity(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value is not None else None

    @field_validator("cosmos_endpoint")
    @classmethod
    def _https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("analytics storage requires an HTTPS endpoint without embedded credentials")
        return value


def run_worker(
    settings: AnalyticsWorkerSettings | None = None,
    *,
    credential: TokenCredential | None = None,
    cosmos_client: Any = None,
    usage_reader: CosmosUsageReader | None = None,
    force_full: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    try:
        settings = settings or AnalyticsWorkerSettings()
    except ValidationError:
        logger.error("Analytics worker configuration is invalid or incomplete.")
        return 2
    owned_credential = credential is None
    owned_cosmos = cosmos_client is None
    with ExitStack() as stack:
        credential = credential or ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
        if owned_credential:
            stack.callback(credential.close)
        try:
            cosmos = cosmos_client or CosmosClient(settings.cosmos_endpoint, credential=credential)
            if owned_cosmos:
                stack.callback(cosmos.close)
            data = PortalDataService(settings.hub_config, credential, usage_reader=usage_reader)
            stack.callback(data.close)
            store = AnalyticsStore(
                cosmos.get_database_client(settings.cosmos_database).get_container_client(
                    settings.analytics_container
                ),
                settings.hub_config.scope,
            )
            worker = AnalyticsMaterializer(store, data, retention_days=settings.analytics_retention_days)
            started = monotonic()
            steps = 0
            while monotonic() - started < settings.analytics_max_work_seconds:
                result = worker.step(force_full=force_full and steps == 0)
                steps += 1
                if result in {"published", "idle", "busy"}:
                    logger.info("Analytics worker finished: outcome=%s steps=%d.", result, steps)
                    return 0
                if result == "failed":
                    logger.error("Analytics generation failed; no partial generation was published.")
                    return 1
            worker.release()
            logger.info("Analytics worker yielded at its durable checkpoint after %d steps.", steps)
            return 0
        except AnalyticsConflict:
            logger.info("A competing analytics worker owns the current checkpoint.")
            return 0
        except (AzureError, AnalyticsUnavailable) as exc:
            logger.error(
                "Analytics dependency is unavailable (%s); committed progress is retained.",
                type(exc).__name__,
            )
            return 1
