# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Backend contract intent (``bicep/infra/llm-backend-onboarding``, backend-contract-version 1.x).

A backend contract declares every LLM backend the gateway routes to. The upstream parameter file
replaces the whole configuration on each deployment, so there is exactly one authoritative backend
contract per environment and changes are always merged into it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from citadel_agent.contracts.common import (
    CircuitBreakerSettings,
    CitadelModel,
    ContractMetadata,
    HttpsUrl,
    ModelName,
    UpstreamShapedModel,
    check_secret_ingress,
)

BackendType = Literal[
    "ai-foundry",
    "azure-openai",
    "aws-bedrock",
    "aws-bedrock-mantle",
    "gemini",
    "gemini-openai",
    "anthropic",
    "azure-flux",
    "azure-mai",
    "external",
]
AuthType = Literal[
    "managed-identity",
    "aws-sigv4",
    "api-key-bearer",
    "api-key-header",
    "api-key-gemini",
    "api-key-anthropic",
    "none",
]
API_KEY_AUTH_TYPES = frozenset({"api-key-bearer", "api-key-header", "api-key-gemini", "api-key-anthropic"})

# Wire-protocol family per backend type. Model aliases may only combine members that speak the
# same protocol, because the gateway rewrites only the request's "model" field.
WIRE_FAMILIES: dict[str, str] = {
    "ai-foundry": "openai",
    "azure-openai": "openai",
    "aws-bedrock-mantle": "openai",
    "gemini-openai": "openai",
    "external": "openai",
    "aws-bedrock": "bedrock-converse",
    "gemini": "gemini-native",
    "anthropic": "anthropic-messages",
    "azure-flux": "image",
    "azure-mai": "image",
}


class ModelEntry(UpstreamShapedModel):
    """A model served by a backend (``supportedModels[]``). Omitted fields use upstream defaults."""

    name: ModelName
    sku: str | None = None
    capacity: int | None = Field(default=None, ge=1)
    model_format: str | None = None
    model_version: str | None = None
    retirement_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    api_version: str | None = None
    timeout: int | None = Field(default=None, ge=1, le=3600)
    inference_api_version: str | None = None
    session_aware_model: bool | None = None
    model_path: str | None = None


class BackendAuthConfig(CitadelModel):
    """Credential reference for API-key backends. Secrets come from Key Vault, never inline."""

    named_value_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]+$")
    key_vault_secret_uri: HttpsUrl | None = None
    header_name: str | None = None


class BackendEntry(UpstreamShapedModel):
    """One LLM backend (``llmBackendConfig[]``)."""

    backend_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    backend_type: BackendType
    endpoint: HttpsUrl
    auth_type: AuthType | None = None
    auth_config: BackendAuthConfig | None = None
    supported_models: list[ModelEntry] = Field(min_length=1)
    priority: int | None = Field(default=None, ge=1, le=5)
    weight: int | None = Field(default=None, ge=1, le=1000)
    circuit_breaker: CircuitBreakerSettings | None = None
    session_affinity: dict[str, object] | None = None

    @model_validator(mode="after")
    def _auth_and_models(self) -> BackendEntry:
        if self.auth_type in API_KEY_AUTH_TYPES:
            config = self.auth_config
            if config is None or not config.named_value_key or not config.key_vault_secret_uri:
                raise ValueError(
                    f"Backend '{self.backend_id}' uses {self.auth_type}: set authConfig.namedValueKey and "
                    "authConfig.keyVaultSecretUri (inline secrets are not allowed)"
                )
        names = [m.name.lower() for m in self.supported_models]
        if len(names) != len(set(names)):
            raise ValueError(f"Backend '{self.backend_id}' lists a model more than once")
        if self.backend_type == "azure-flux":
            missing = [m.name for m in self.supported_models if not m.model_path]
            if missing:
                raise ValueError(f"azure-flux models need modelPath: {', '.join(missing)}")
        return self

    @property
    def wire_family(self) -> str:
        return WIRE_FAMILIES[self.backend_type]


class ModelAlias(CitadelModel):
    """A client-facing model name that resolves to one of several real models."""

    name: ModelName
    models: list[ModelName] = Field(min_length=1)
    strategy: Literal["priority", "weighted"] = "priority"
    weights: list[int] | None = None

    @model_validator(mode="after")
    def _weights(self) -> ModelAlias:
        if self.strategy == "weighted":
            if not self.weights or len(self.weights) != len(self.models):
                raise ValueError(f"Alias '{self.name}': weighted strategy needs one weight per model")
            if any(w <= 0 for w in self.weights):
                raise ValueError(f"Alias '{self.name}': weights must be positive")
        elif self.weights:
            raise ValueError(f"Alias '{self.name}': weights are only valid with the weighted strategy")
        if len({m.lower() for m in self.models}) != len(self.models):
            raise ValueError(f"Alias '{self.name}' lists a model more than once")
        return self


class BackendContractIntent(CitadelModel):
    """Intent for the single backend contract of an environment (``contracts/backend/<env>/``)."""

    kind: Literal["backend-contract"] = "backend-contract"
    schema_version: Literal[1] = 1
    environment: str = Field(min_length=1)
    metadata: ContractMetadata = Field(default_factory=ContractMetadata)
    backends: list[BackendEntry] = Field(min_length=1)
    model_aliases: list[ModelAlias] = Field(default_factory=list)
    circuit_breaker_enabled: bool = True
    circuit_breaker_defaults: CircuitBreakerSettings | None = None
    session_affinity_enabled: bool = True
    session_affinity_defaults: dict[str, object] | None = None
    anthropic_version: str | None = None
    # AWS SigV4 credentials are plain (non-secure) upstream parameters, so they are never rendered
    # into files. "pipeline-secret" means the CD pipeline injects them at deployment time.
    aws_credentials_source: Literal["none", "pipeline-secret"] = "none"
    extra_parameters: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_contract(self) -> BackendContractIntent:
        ids = [b.backend_id.lower() for b in self.backends]
        if len(ids) != len(set(ids)):
            raise ValueError("backendId values must be unique")
        if (
            any(b.backend_type == "aws-bedrock" for b in self.backends)
            and self.aws_credentials_source == "none"
        ):
            raise ValueError("aws-bedrock backends need awsCredentialsSource: pipeline-secret")
        self._validate_aliases()
        check_secret_ingress(self, "Backend contract intent")
        return self

    def model_index(self) -> dict[str, list[BackendEntry]]:
        """Map lower-cased model names to the backends that serve them."""
        index: dict[str, list[BackendEntry]] = {}
        for backend in self.backends:
            for model in backend.supported_models:
                index.setdefault(model.name.lower(), []).append(backend)
        return index

    def _validate_aliases(self) -> None:
        index = self.model_index()
        alias_names = [a.name.lower() for a in self.model_aliases]
        if len(alias_names) != len(set(alias_names)):
            raise ValueError("Alias names must be unique")
        for alias in self.model_aliases:
            if alias.name.lower() in index:
                raise ValueError(f"Alias '{alias.name}' collides with a real model name")
            families: set[str] = set()
            for member in alias.models:
                backends = index.get(member.lower())
                if not backends:
                    raise ValueError(f"Alias '{alias.name}' references unknown model '{member}'")
                families.update(b.wire_family for b in backends)
            if len(families) > 1:
                raise ValueError(
                    f"Alias '{alias.name}' mixes wire protocols ({', '.join(sorted(families))}); "
                    "all members must speak the same protocol"
                )

    def pooled_models(self) -> dict[str, list[str]]:
        """Models served by two or more backends (upstream creates a backend pool for each)."""
        return {
            name: [b.backend_id for b in backends]
            for name, backends in self.model_index().items()
            if len(backends) > 1
        }
