# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Access contract intent (``bicep/infra/citadel-access-contracts``, access-contract-version 1.x).

An access contract onboards a use case: one APIM product, subscription and product policy per
service code, plus optional Key Vault secrets, Foundry connections and multi-gateway mirrors.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, PositiveInt, StringConstraints, field_validator, model_validator

from citadel_agent.contracts.common import (
    AzureResourceRef,
    CitadelModel,
    ContractMetadata,
    FoundryProjectRef,
    HttpsUrl,
    ModelName,
    NameSegment,
    SecretName,
    check_deployment_name,
    check_secret_ingress,
)

KNOWN_LLM_APIS: tuple[str, ...] = ("universal-llm-api", "azure-openai-api", "unified-ai-api")
MULTI_ASSET_CODES = frozenset({"TOOL", "AGENT", "MULTI"})
DEFAULT_FOUNDRY_AUDIENCE = "https://cognitiveservices.azure.com"
# Free-text values embedded in policies are limited to characters that need no escaping. '@' is
# excluded because APIM treats attribute values that start with '@' as policy expressions.
SAFE_LITERAL_PATTERN = r"^[A-Za-z0-9 ._:/-]*$"
# JWT audiences and issuers: an absolute URI (no quotes, angle brackets or '@') or an application ID.
URI_OR_GUID_PATTERN = (
    r"^(?:[A-Za-z][A-Za-z0-9+.-]*://[^\s\"<>@{}]+|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$"
)

QuotaPeriod = Literal["Hourly", "Daily", "Weekly", "Monthly", "Yearly"]
ContentCategory = Literal["Hate", "Sexual", "SelfHarm", "Violence"]


class UseCase(CitadelModel):
    """Naming of the use case: products are ``<code>-<businessUnit>-<useCaseName>-<environment>``."""

    business_unit: NameSegment
    use_case_name: NameSegment
    environment: NameSegment

    @property
    def postfix(self) -> str:
        return f"{self.business_unit}-{self.use_case_name}-{self.environment}"


class AssetEndpoint(CitadelModel):
    """Publish a Key Vault endpoint secret for one granted API (empty name = auto-generated)."""

    api_name: str = Field(min_length=1)
    endpoint_secret_name: SecretName | None = None


class TokenLimit(CitadelModel):
    """``llm-token-limit``: tokens per minute plus an optional long-term token quota."""

    tokens_per_minute: PositiveInt
    token_quota: PositiveInt | None = None
    token_quota_period: QuotaPeriod | None = None
    estimate_prompt_tokens: bool = False

    @model_validator(mode="after")
    def _quota_pair(self) -> TokenLimit:
        if (self.token_quota is None) != (self.token_quota_period is None):
            raise ValueError("tokenQuota and tokenQuotaPeriod must be set together")
        return self


class ModelTokenLimit(TokenLimit):
    """A token limit that applies to one model (or alias) only."""

    model: ModelName


class CallLimit(CitadelModel):
    """``rate-limit-by-key`` plus optional ``quota-by-key`` for tools (MCP) or agents (A2A)."""

    calls: PositiveInt
    renewal_period_seconds: PositiveInt = 60
    quota_calls: PositiveInt | None = None
    quota_period_seconds: PositiveInt | None = None

    @model_validator(mode="after")
    def _quota_pair(self) -> CallLimit:
        if (self.quota_calls is None) != (self.quota_period_seconds is None):
            raise ValueError("quotaCalls and quotaPeriodSeconds must be set together")
        return self


class ContentSafetyPolicy(CitadelModel):
    """``llm-content-safety`` settings. Thresholds use the eight-level scale (0 strictest, 7 most lenient)."""

    enabled: bool = False
    shield_prompt: bool = True
    enforce_on_completions: bool = False
    thresholds: dict[ContentCategory, int] = Field(
        default_factory=lambda: {"Hate": 3, "Sexual": 3, "SelfHarm": 3, "Violence": 3}
    )

    @field_validator("thresholds")
    @classmethod
    def _threshold_range(cls, value: dict[str, int]) -> dict[str, int]:
        for category, threshold in value.items():
            if not 0 <= threshold <= 7:
                raise ValueError(f"Threshold for {category} must be between 0 and 7")
        return value


class RegexPattern(CitadelModel):
    pattern: str = Field(min_length=1, max_length=500)
    category: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")


class PiiPolicy(CitadelModel):
    """PII protection through Azure AI Language: anonymize (mask and restore) or block requests."""

    mode: Literal["off", "anonymize", "block"] = "off"
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    excluded_categories: list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]+$")]] = Field(
        default_factory=lambda: ["PersonType"]
    )
    language: str = Field(default="en", pattern=r"^(auto|[a-z]{2,3}(-[A-Za-z]{2,4})?)$")
    regex_patterns: list[RegexPattern] = Field(default_factory=list)
    save_state: bool = False
    max_chunk_size: int = Field(default=5000, ge=500, le=5120)
    chunk_overlap: int = Field(default=250, ge=0)

    @model_validator(mode="after")
    def _overlap(self) -> PiiPolicy:
        if self.chunk_overlap > self.max_chunk_size // 2:
            raise ValueError("chunkOverlap must be at most half of maxChunkSize")
        return self


class JwtPolicy(CitadelModel):
    """Layered Microsoft Entra ID (or other OIDC) JWT validation on top of the subscription key."""

    required: bool = False
    required_roles: list[str] = Field(default_factory=list)
    audience: str | None = Field(default=None, pattern=URI_OR_GUID_PATTERN)
    issuer: str | None = Field(default=None, pattern=URI_OR_GUID_PATTERN)
    openid_config_url: HttpsUrl | None = None

    @field_validator("required_roles")
    @classmethod
    def _role_format(cls, roles: list[str]) -> list[str]:
        for role in roles:
            if not role or "," in role or " " in role:
                raise ValueError(f"Invalid app role '{role}'")
        return roles


class AlertToggles(CitadelModel):
    """Per-contract ``raise-alert-events`` toggles (defaults match upstream)."""

    enabled: bool = True
    throttling: bool = True
    quota_exceeded: bool = True
    backend_failure: bool = True
    auth_failure: bool = False
    content_safety: bool = False
    pii_failure: bool = False


class UsageDimension(CitadelModel):
    """Source of a usage custom dimension: a request header or a static value."""

    header: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]+$")
    static_value: str | None = Field(default=None, max_length=128, pattern=SAFE_LITERAL_PATTERN)
    default: str = Field(default="NA", max_length=128, pattern=SAFE_LITERAL_PATTERN)

    @model_validator(mode="after")
    def _one_source(self) -> UsageDimension:
        if (self.header is None) == (self.static_value is None):
            raise ValueError("Set exactly one of header or staticValue")
        return self


class ProductPolicyIntent(CitadelModel):
    """High-level product policy. Rendered to APIM policy XML by the policy builder."""

    allowed_models: list[ModelName] = Field(default_factory=list)
    allowed_backend_pools: list[str] = Field(default_factory=list)
    token_limit: TokenLimit | None = None
    model_token_limits: list[ModelTokenLimit] = Field(default_factory=list)
    tool_limits: CallLimit | None = None
    agent_limits: CallLimit | None = None
    content_safety: ContentSafetyPolicy = Field(default_factory=ContentSafetyPolicy)
    pii: PiiPolicy = Field(default_factory=PiiPolicy)
    jwt: JwtPolicy = Field(default_factory=JwtPolicy)
    alerts: AlertToggles = Field(default_factory=AlertToggles)
    custom_dimension1: UsageDimension | None = None
    custom_dimension2: UsageDimension | None = None
    response_headers: bool = False

    @model_validator(mode="after")
    def _model_limits(self) -> ProductPolicyIntent:
        names = [limit.model.lower() for limit in self.model_token_limits]
        if len(names) != len(set(names)):
            raise ValueError("modelTokenLimits contains duplicate models")
        if self.allowed_models:
            allowed = {m.lower() for m in self.allowed_models}
            unknown = [limit.model for limit in self.model_token_limits if limit.model.lower() not in allowed]
            if unknown:
                raise ValueError(
                    f"modelTokenLimits reference models that are not allowed: {', '.join(unknown)}"
                )
        return self


class ServiceIntent(CitadelModel):
    """One service code of the contract: one APIM product, subscription and key."""

    code: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,15}$")
    apis: list[str] = Field(min_length=1)
    # Which granted APIs are published Tools (MCP) and Agents (A2A); the rest are LLM APIs.
    tool_apis: list[str] = Field(default_factory=list)
    agent_apis: list[str] = Field(default_factory=list)
    key_secret_name: SecretName
    endpoint_secret_name: SecretName | None = None
    asset_endpoints: list[AssetEndpoint] = Field(default_factory=list)
    publish_all_asset_endpoints: bool = False
    foundry_api_name: str | None = None
    policy: ProductPolicyIntent = Field(default_factory=ProductPolicyIntent)
    # Escape hatch: a hand-authored policy file in the contract folder. The agent renders it
    # verbatim and does not manage its semantics.
    policy_xml_file: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]+\.xml$")

    @field_validator("apis")
    @classmethod
    def _unique_apis(cls, apis: list[str]) -> list[str]:
        if len(apis) != len(set(apis)):
            raise ValueError("apis contains duplicates")
        return apis

    @model_validator(mode="after")
    def _references(self) -> ServiceIntent:
        unknown = [e.api_name for e in self.asset_endpoints if e.api_name not in self.apis]
        if unknown:
            raise ValueError(
                f"assetEndpoints reference APIs not granted to {self.code}: {', '.join(unknown)}"
            )
        if self.foundry_api_name and self.foundry_api_name not in self.apis:
            raise ValueError(f"foundryApiName '{self.foundry_api_name}' is not granted to {self.code}")
        if (self.policy.tool_limits or self.policy.agent_limits) and self.code not in MULTI_ASSET_CODES:
            raise ValueError("toolLimits and agentLimits apply only to TOOL, AGENT or MULTI services")
        classified = [*self.tool_apis, *self.agent_apis]
        ungranted = [api for api in classified if api not in self.apis]
        if ungranted:
            raise ValueError(
                f"toolApis/agentApis reference APIs not granted to {self.code}: {', '.join(ungranted)}"
            )
        if len(classified) != len(set(classified)):
            raise ValueError("An API cannot be both a tool and an agent")
        if classified and self.code not in MULTI_ASSET_CODES:
            raise ValueError("toolApis and agentApis apply only to TOOL, AGENT or MULTI services")
        return self

    @property
    def is_multi_asset(self) -> bool:
        return self.code in MULTI_ASSET_CODES

    @property
    def llm_apis(self) -> list[str]:
        return [api for api in self.apis if api in KNOWN_LLM_APIS]

    @property
    def endpoint_secret_count(self) -> int:
        """Number of endpoint secrets upstream publishes for this service."""
        per_asset = len(self.apis) if self.publish_all_asset_endpoints else len(self.asset_endpoints)
        return per_asset + (1 if self.endpoint_secret_name else 0)


class FoundryConnectionIntent(CitadelModel):
    """Optional Microsoft Foundry project connection to the gateway (LLM endpoint only)."""

    enabled: bool = False
    project: FoundryProjectRef | None = None
    connection_name_prefix: str = Field(default="", pattern=r"^[A-Za-z0-9-]*$")
    connection_category: Literal["ApiManagement", "ModelGateway"] = "ApiManagement"
    auth_type: Literal["ProjectManagedIdentity", "ApiKey"] = "ProjectManagedIdentity"
    managed_identity_audience: str = DEFAULT_FOUNDRY_AUDIENCE
    deployment_in_path: bool = False
    is_shared_to_all: bool = False
    inference_api_version: str = ""
    deployment_api_version: str = ""
    static_models: list[dict[str, object]] = Field(default_factory=list)
    list_models_endpoint: str = ""
    get_model_endpoint: str = ""
    deployment_provider: Literal["", "AzureOpenAI", "OpenAI"] = ""
    custom_headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _project_required(self) -> FoundryConnectionIntent:
        if self.enabled and self.project is None:
            raise ValueError("A Foundry project is required when the Foundry connection is enabled")
        if any(name.lower() in {"api-key", "authorization"} for name in self.custom_headers):
            raise ValueError("customHeaders must not set api-key or Authorization (credentials are injected)")
        return self


class MirrorKeyVault(AzureResourceRef):
    endpoint_source: str = ""


class MirrorFoundry(FoundryProjectRef):
    endpoint_source: str = ""


class ResiliencyIntent(CitadelModel):
    """Mirror the contract into additional gateways, Key Vaults and Foundry projects."""

    global_gateway_url: HttpsUrl | Literal[""] = ""
    additional_apim_gateways: list[AzureResourceRef] = Field(default_factory=list)
    additional_key_vaults: list[MirrorKeyVault] = Field(default_factory=list)
    additional_foundries: list[MirrorFoundry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _endpoint_sources(self) -> ResiliencyIntent:
        sources = [m.endpoint_source for m in self.additional_key_vaults] + [
            m.endpoint_source for m in self.additional_foundries
        ]
        for source in sources:
            if source in ("", "primary"):
                continue
            if source == "global":
                if not self.global_gateway_url:
                    raise ValueError("endpointSource 'global' requires globalGatewayUrl")
                continue
            if source.startswith("secondary:") and source.removeprefix("secondary:").isdigit():
                if int(source.removeprefix("secondary:")) >= len(self.additional_apim_gateways):
                    raise ValueError(f"endpointSource '{source}' points past additionalApimGateways")
                continue
            raise ValueError(
                f"Invalid endpointSource '{source}' (use '', 'global', 'primary' or 'secondary:<index>')"
            )
        return self


class KeyRotationState(CitadelModel):
    """Key-rotation parameters. Changed only by the key-rotation workflow."""

    use_primary_key: bool = True
    key_rotation_enabled: bool = False


class AccessContractIntent(CitadelModel):
    """Intent for one access contract (``contracts/access/<bu>/<use-case>/<env>/contract.yaml``)."""

    kind: Literal["access-contract"] = "access-contract"
    schema_version: Literal[1] = 1
    environment: str = Field(
        min_length=1, description="Admin-policy environment key (resolves Azure coordinates)"
    )
    metadata: ContractMetadata = Field(default_factory=ContractMetadata)
    use_case: UseCase
    services: list[ServiceIntent] = Field(min_length=1)
    store_credentials_in_key_vault: bool = True
    foundry: FoundryConnectionIntent = Field(default_factory=FoundryConnectionIntent)
    resiliency: ResiliencyIntent = Field(default_factory=ResiliencyIntent)
    key_rotation: KeyRotationState = Field(default_factory=KeyRotationState)
    product_terms: str = ""
    extra_parameters: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_contract(self) -> AccessContractIntent:
        codes = [s.code for s in self.services]
        if len(codes) != len(set(codes)):
            raise ValueError("Each service code may appear only once per contract")
        for name in self.nested_deployment_names():
            check_deployment_name(name)
        check_secret_ingress(self, "Access contract intent")
        return self

    def product_id(self, code: str) -> str:
        return f"{code}-{self.use_case.postfix}"

    def product_display_name(self, code: str) -> str:
        uc = self.use_case
        return f"{code} {uc.business_unit} {uc.use_case_name} {uc.environment}"

    def subscription_name(self, code: str) -> str:
        return f"{self.product_id(code)}-SUB-01"

    def key_vault_secret_names(self) -> set[str]:
        """Key Vault secrets the contract writes, normalized like upstream (lowercase, '_' to '-')."""
        names: set[str] = set()
        if not self.store_credentials_in_key_vault:
            return names
        for service in self.services:
            for name in (service.key_secret_name, service.endpoint_secret_name):
                if name:
                    names.add(name.lower().replace("_", "-"))
        return names

    @property
    def foundry_connection_prefix(self) -> str:
        return self.foundry.connection_name_prefix or f"Hub-{self.use_case.postfix}"

    def foundry_connection_name(self, code: str) -> str:
        return f"{self.foundry_connection_prefix}-{code}"

    def nested_deployment_names(self) -> list[str]:
        """Names of the nested deployments the upstream template creates (each must be <= 64 chars)."""
        postfix = self.use_case.postfix
        names: list[str] = []
        secret_items = sum(1 + s.endpoint_secret_count for s in self.services)
        for service in self.services:
            names.append(f"onboard-{service.code}-{postfix}")
            if self.foundry.enabled:
                names.append(f"foundry-{service.code}-{postfix}")
            for index in range(len(self.resiliency.additional_apim_gateways)):
                names.append(f"onboard-add{index}-{service.code}-{postfix}")
            for index in range(len(self.resiliency.additional_foundries)):
                names.append(f"foundry-add{index}-{service.code}-{postfix}")
        if self.store_credentials_in_key_vault:
            names.append(f"kv-p{max(secret_items - 1, 0)}-{postfix}")
        if self.resiliency.additional_key_vaults:
            names.append(f"kv-add-{secret_items * len(self.resiliency.additional_key_vaults) - 1}-{postfix}")
        return names
