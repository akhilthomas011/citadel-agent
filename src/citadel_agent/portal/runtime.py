# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Production construction of portal BFF and private agent apps from environment settings."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from azure.core.credentials import TokenCredential
from azure.cosmos import CosmosClient
from azure.identity import ManagedIdentityCredential
from fastapi import FastAPI
from msal import ConfidentialClientApplication
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from citadel_agent.azure_clients.keyvault import KeyVaultReader
from citadel_agent.azure_clients.monitor import AlertReader
from citadel_agent.portal.api import create_agent_app
from citadel_agent.portal.auth import (
    EntraServiceTokenValidator,
    EntraTokenSettings,
    EntraTokenValidator,
)
from citadel_agent.portal.auth_web import MsalOAuthClient, PortalAuthConfig
from citadel_agent.portal.bff import (
    HttpxAgentJobStatusClient,
    ManagedIdentityAgentTokenProvider,
    create_bff_app,
)
from citadel_agent.portal.grants import CosmosHubGrantStore
from citadel_agent.portal.jobs import CosmosPortalJobStore
from citadel_agent.portal.product_contracts import PortalHubConfig
from citadel_agent.portal.sessions import CosmosContainer, CosmosPortalSessionStore


class PortalRuntimeSettings(BaseSettings):
    """Required Azure and Entra settings for hosted BFF and agent processes."""

    model_config = SettingsConfigDict(
        env_prefix="CITADEL_PORTAL_",
        env_file=".env",
        extra="ignore",
    )

    tenant_id: str
    client_id: str
    api_audience: str
    delegated_scope: str = "api://citadel-portal/access_as_user"
    delegated_scope_name: str = "access_as_user"
    client_secret_name: str
    key_vault_url: str
    cosmos_endpoint: str
    cosmos_database: str
    cosmos_container: str
    analytics_container: str | None = Field(default=None, min_length=1, max_length=128)
    durable_turns_enabled: bool = False
    assistant_worker_max_seconds: int = Field(default=240, ge=10, le=1200)
    portal_origin: str
    callback_uri: str
    agent_api_url: str
    agent_api_audience: str
    agent_api_token_scope: str | None = None
    bff_service_client_id: str | None = None
    managed_identity_client_id: str | None = None
    hub_config: PortalHubConfig | None = None
    static_directory: Path | None = None
    session_lifetime_seconds: int = Field(default=900, ge=60, le=3600)
    auth_flow_lifetime_seconds: int = Field(default=300, ge=60, le=600)

    @field_validator(
        "key_vault_url",
        "cosmos_endpoint",
        "portal_origin",
        "callback_uri",
        "agent_api_url",
    )
    @classmethod
    def _require_https_urls(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("hosted portal endpoints must be valid HTTPS URLs")
        return value.rstrip("/") if value.endswith("/") and parsed.path == "" else value

    @field_validator("tenant_id", "client_id")
    @classmethod
    def _canonicalize_identity_ids(cls, value: str) -> str:
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError("tenant_id and client_id must be UUIDs") from exc

    @field_validator("managed_identity_client_id", "bff_service_client_id")
    @classmethod
    def _canonicalize_managed_identity_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError("managed_identity_client_id must be a UUID") from exc

    @field_validator(
        "client_id",
        "api_audience",
        "delegated_scope",
        "delegated_scope_name",
        "client_secret_name",
        "cosmos_database",
        "cosmos_container",
        "agent_api_audience",
    )
    @classmethod
    def _require_nonempty_settings(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("portal runtime settings must not be empty")
        return value

    def auth_config(self) -> PortalAuthConfig:
        return PortalAuthConfig(
            origin=self.portal_origin,
            callback_uri=self.callback_uri,
            session_lifetime_seconds=self.session_lifetime_seconds,
            auth_flow_lifetime_seconds=self.auth_flow_lifetime_seconds,
        )

    def delegated_token_settings(self) -> EntraTokenSettings:
        return EntraTokenSettings(
            tenant_id=self.tenant_id,
            audience=self.api_audience,
            allowed_client_ids=frozenset({self.client_id}),
            required_scope=self.delegated_scope_name,
        )

    def service_token_settings(self) -> EntraTokenSettings:
        return EntraTokenSettings(
            tenant_id=self.tenant_id,
            audience=self.agent_api_audience,
            allowed_client_ids=frozenset({self.bff_service_client_id or self.client_id}),
        )


def create_bff_app_from_settings(
    settings: PortalRuntimeSettings | None = None,
    *,
    credential: TokenCredential | None = None,
    cosmos_client: CosmosClient | None = None,
    secret_reader: KeyVaultReader | None = None,
    agent_http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Create the browser BFF and resolve its existing Cosmos state and Key Vault secret."""
    settings = settings or PortalRuntimeSettings()
    auth_config = settings.auth_config()
    azure_credential, cosmos, container, owned_credential, owned_cosmos = _cloud_state(
        settings,
        credential=credential,
        cosmos_client=cosmos_client,
    )
    if secret_reader is None:
        secret_reader = KeyVaultReader(azure_credential, settings.key_vault_url)
    client_secret = secret_reader.get_secret_in_memory(settings.client_secret_name)
    if not client_secret:
        raise RuntimeError("The configured Entra confidential-client secret is empty.")

    oauth_application = ConfidentialClientApplication(
        client_id=settings.client_id,
        authority=f"https://login.microsoftonline.com/{settings.tenant_id}",
        client_credential=client_secret,
    )
    oauth_client = MsalOAuthClient(
        oauth_application,
        scopes=(settings.delegated_scope,),
        redirect_uri=settings.callback_uri,
    )
    session_store = CosmosPortalSessionStore(container)
    http_client = agent_http_client
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=httpx.Timeout(330, connect=10), trust_env=False)
    agent_client = HttpxAgentJobStatusClient(http_client, settings.agent_api_url)
    app = create_bff_app(
        auth_config=auth_config,
        oauth_client=oauth_client,
        delegated_token_validator=EntraTokenValidator(settings.delegated_token_settings()),
        session_store=session_store,
        agent_token_provider=ManagedIdentityAgentTokenProvider(
            azure_credential,
            settings.agent_api_audience,
            token_scope=settings.agent_api_token_scope,
        ),
        agent_client=agent_client,
        product_client=agent_client,
        static_directory=settings.static_directory,
        readiness_probe=container.read,
    )
    _register_shutdown(
        app,
        owned_credential=azure_credential if owned_credential else None,
        owned_cosmos=cosmos if owned_cosmos else None,
        owned_http_client=http_client if agent_http_client is None else None,
    )
    return app


def create_agent_app_from_settings(
    settings: PortalRuntimeSettings | None = None,
    *,
    credential: TokenCredential | None = None,
    cosmos_client: CosmosClient | None = None,
) -> FastAPI:
    """Create the private agent API using a managed identity and shared application state."""
    settings = settings or PortalRuntimeSettings()
    azure_credential, cosmos, container, owned_credential, owned_cosmos = _cloud_state(
        settings,
        credential=credential,
        cosmos_client=cosmos_client,
    )
    product = None
    grants = CosmosHubGrantStore(container)
    if settings.hub_config is not None:
        from citadel_agent.analytics.read_model import AnalyticsReader
        from citadel_agent.analytics.snapshots import AnalyticsStore, source_fingerprint
        from citadel_agent.portal.assistant import HostedRuntimeFactory, PortalAssistantService
        from citadel_agent.portal.data import PortalDataService
        from citadel_agent.portal.hosted_grants import DeploymentHubGrantReader
        from citadel_agent.portal.product_api import PortalProduct
        from citadel_agent.portal.state import ProductStore

        config = settings.hub_config
        if config.scope.tenant_id != settings.tenant_id:
            raise ValueError("hub tenant must match the trusted portal tenant")
        store = ProductStore(container, config.scope)
        token = None
        if config.github_token_secret_name:
            token = KeyVaultReader(azure_credential, settings.key_vault_url).get_secret_in_memory(
                config.github_token_secret_name
            )
            if not token:
                raise RuntimeError("The contracts GitHub credential is empty.")
        factory = HostedRuntimeFactory(config, azure_credential, store, github_token=token)
        grants = DeploymentHubGrantReader(grants, config)
        data_service = PortalDataService(config, azure_credential, canonical_counts=factory.canonical_counts)
        product = PortalProduct(
            config=config,
            grant_store=grants,
            data=data_service,
            assistant=PortalAssistantService(
                store,
                factory,
                data_service=data_service,
                durable_turns=settings.durable_turns_enabled,
                worker_sessions=CosmosPortalSessionStore(container),
                worker_grants=grants,
            ),
            alert_reader=AlertReader(azure_credential),
            analytics=(
                AnalyticsReader(
                    AnalyticsStore(
                        cosmos.get_database_client(settings.cosmos_database).get_container_client(
                            settings.analytics_container
                        ),
                        config.scope,
                    ),
                    source_fingerprint(config),
                )
                if settings.analytics_container is not None
                else None
            ),
        )

    def readiness():
        container.read()
        if settings.durable_turns_enabled and (product is None or not product.assistant.worker_available()):
            from azure.core.exceptions import AzureError

            raise AzureError("The durable assistant worker has no recent heartbeat.")

    app = create_agent_app(
        service_token_validator=EntraServiceTokenValidator(settings.service_token_settings()),
        session_store=CosmosPortalSessionStore(container),
        grant_store=grants,
        job_store=CosmosPortalJobStore(container),
        readiness_probe=readiness,
        product=product,
    )
    if product is not None:
        app.router.add_event_handler("shutdown", product.data.close)
        if product.alert_reader is not None:
            app.router.add_event_handler("shutdown", product.alert_reader.close)
    _register_shutdown(
        app,
        owned_credential=azure_credential if owned_credential else None,
        owned_cosmos=cosmos if owned_cosmos else None,
    )
    return app


def _cloud_state(
    settings: PortalRuntimeSettings,
    *,
    credential: TokenCredential | None,
    cosmos_client: CosmosClient | None,
) -> tuple[TokenCredential, CosmosClient, CosmosContainer, bool, bool]:
    owned_credential = credential is None
    azure_credential = credential
    if azure_credential is None:
        azure_credential = ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
    owned_cosmos = cosmos_client is None
    cosmos = cosmos_client
    if cosmos is None:
        cosmos = CosmosClient(settings.cosmos_endpoint, credential=azure_credential)
    container = cosmos.get_database_client(settings.cosmos_database).get_container_client(
        settings.cosmos_container
    )
    return azure_credential, cosmos, container, owned_credential, owned_cosmos


def _register_shutdown(
    app: FastAPI,
    *,
    owned_credential: TokenCredential | None,
    owned_cosmos: CosmosClient | None,
    owned_http_client: httpx.AsyncClient | None = None,
) -> None:
    if owned_credential is not None:
        app.router.add_event_handler("shutdown", owned_credential.close)
    if owned_cosmos is not None:
        app.router.add_event_handler("shutdown", owned_cosmos.close)
    if owned_http_client is not None:
        app.router.add_event_handler("shutdown", owned_http_client.aclose)
