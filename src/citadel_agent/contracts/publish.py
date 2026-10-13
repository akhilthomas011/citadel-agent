# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Publish contract intent (``bicep/infra/citadel-publish-contracts``, publish-contract 1.1.0-preview).

Publishing exposes Tools (MCP) and Agents (A2A) through the gateway. It does not grant access:
consumers still need an access contract that includes the published API names.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from citadel_agent.contracts.common import (
    CircuitBreakerSettings,
    CitadelModel,
    ContractMetadata,
    HttpsUrl,
    UpstreamShapedModel,
    check_secret_ingress,
)

AssetType = Literal["mcp-from-api", "mcp-existing", "a2a"]
ASSET_TYPE_PREFIX: dict[str, str] = {"mcp-from-api": "mcp", "mcp-existing": "mcp", "a2a": "agent"}


class AssetMetadata(UpstreamShapedModel):
    version: str | None = None
    owner: str | None = None
    contact_email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    compliance: list[str] = Field(default_factory=list)
    classification: Literal["public", "internal", "confidential"] | None = None


class PublishBackendAuthConfig(CitadelModel):
    """Backend credential reference. API keys come from Key Vault, never inline."""

    resource: str | None = None
    header_name: str | None = None
    named_value_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]+$")
    key_vault_secret_uri: HttpsUrl | None = None


class PublishBackend(UpstreamShapedModel):
    url: HttpsUrl
    auth_type: Literal["none", "managed-identity", "api-key-header", "api-key-bearer"] = "none"
    auth_config: PublishBackendAuthConfig | None = None
    circuit_breaker: CircuitBreakerSettings | None = None

    @model_validator(mode="after")
    def _auth(self) -> PublishBackend:
        config = self.auth_config
        if self.auth_type == "managed-identity" and (config is None or not config.resource):
            raise ValueError(
                "managed-identity backends need authConfig.resource (for example https://ai.azure.com)"
            )
        if self.auth_type in {"api-key-header", "api-key-bearer"} and (
            config is None or not config.named_value_key or not config.key_vault_secret_uri
        ):
            raise ValueError(
                f"{self.auth_type} backends need authConfig.namedValueKey and authConfig.keyVaultSecretUri"
            )
        return self


class ApiCenterRegistration(UpstreamShapedModel):
    environment_name: str | None = None
    lifecycle_stage: str | None = None
    version_name: str | None = None
    version_display_name: str | None = None
    documentation_url: str | None = None
    custom_properties: dict[str, object] = Field(default_factory=dict)


class ApiCenterTarget(CitadelModel):
    subscription_id: str = ""
    resource_group_name: str = ""
    service_name: str = ""
    workspace_name: str = "default"


class PublishAsset(UpstreamShapedModel):
    """One published Tool (MCP) or Agent (A2A)."""

    asset_type: AssetType
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    display_name: str = Field(min_length=1)
    description: str = ""
    path: str = Field(pattern=r"^[a-z0-9][a-z0-9-/]{0,99}$")
    metadata: AssetMetadata = Field(default_factory=AssetMetadata)
    policy_xml_file: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]+\.xml$")
    publish_to_api_center: bool = False
    api_center: ApiCenterRegistration | None = None
    path_prefix: str | None = Field(default=None, pattern=r"^[a-z0-9-]*$")
    # mcp-from-api
    source_api_name: str | None = None
    operation_names: list[str] = Field(default_factory=list)
    forward_subscription_key_to_source: bool | None = None
    source_subscription_key_header_name: str | None = None
    # mcp-existing
    transport_type: Literal["streamable", "sse"] | None = None
    # shared by mcp-existing and a2a
    backend: PublishBackend | None = None
    subscription_required: bool | None = None
    subscription_key_header_name: Literal["api-key", "Ocp-Apim-Subscription-Key"] | None = None
    # a2a
    agent_id: str | None = None
    agent_card_path: str | None = None
    agent_card_backend_url: HttpsUrl | None = None
    json_rpc_path: str | None = None

    @model_validator(mode="after")
    def _type_specific(self) -> PublishAsset:
        if self.asset_type == "mcp-from-api":
            if not self.source_api_name or not self.operation_names:
                raise ValueError(f"Asset '{self.name}': mcp-from-api needs sourceApiName and operationNames")
            if self.backend is not None:
                raise ValueError(
                    f"Asset '{self.name}': mcp-from-api reuses the source API backend; remove backend"
                )
        elif self.asset_type == "mcp-existing":
            if self.backend is None:
                raise ValueError(f"Asset '{self.name}': mcp-existing needs a backend")
        elif self.asset_type == "a2a" and (
            self.backend is None or not self.agent_id or not self.agent_card_backend_url
        ):
            raise ValueError(f"Asset '{self.name}': a2a needs agentId, agentCardBackendUrl and backend")
        return self

    def gateway_path(self, use_asset_type_path_prefix: bool = True) -> str:
        """Return the path under the gateway URL where the asset is served (without the gateway host)."""
        prefix = (
            self.path_prefix
            if self.path_prefix is not None
            else (ASSET_TYPE_PREFIX[self.asset_type] if use_asset_type_path_prefix else "")
        )
        base = f"{prefix}/{self.path}" if prefix else self.path
        return f"{base}/mcp" if self.asset_type == "mcp-from-api" else base


class PublishContractIntent(CitadelModel):
    """Intent for a publish contract (``contracts/publish/<name>/<env>/contract.yaml``)."""

    kind: Literal["publish-contract"] = "publish-contract"
    schema_version: Literal[1] = 1
    environment: str = Field(min_length=1)
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    metadata: ContractMetadata = Field(default_factory=ContractMetadata)
    assets: list[PublishAsset] = Field(min_length=1)
    configure_circuit_breaker: bool = True
    circuit_breaker_defaults: CircuitBreakerSettings | None = None
    ensure_usage_fragments: bool = True
    use_asset_type_path_prefix: bool = True
    api_center: ApiCenterTarget | None = None
    extra_parameters: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_contract(self) -> PublishContractIntent:
        names = [a.name for a in self.assets]
        if len(names) != len(set(names)):
            raise ValueError("Asset names must be unique")
        paths = [a.gateway_path(self.use_asset_type_path_prefix) for a in self.assets]
        if len(paths) != len(set(paths)):
            raise ValueError("Asset gateway paths must be unique")
        if any(a.publish_to_api_center for a in self.assets) and not (
            self.api_center and self.api_center.service_name
        ):
            raise ValueError("publishToApiCenter requires apiCenter.serviceName")
        check_secret_ingress(self, "Publish contract intent")
        return self
