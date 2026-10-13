# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Shared building blocks for contract intent models.

Intents are the agent's source of truth for Citadel contracts. They are stored as YAML
(camelCase keys, matching upstream parameter naming) in the canonical contracts repository and
rendered into the ``.bicepparam`` / policy XML files that upstream Citadel templates consume.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from citadel_agent.safety.secret_ingress import ensure_no_secret_literals

ISO8601_DURATION = re.compile(r"^P(?!$)(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+S)?)?$")
ARM_DEPLOYMENT_NAME_LIMIT = 64


def _check_duration(value: str) -> str:
    # Python's re is used because pydantic-core patterns do not support look-arounds.
    if not ISO8601_DURATION.match(value):
        raise ValueError(f"'{value}' is not an ISO 8601 duration such as PT5M or PT30S")
    return value


# Names used to build APIM product IDs, deployment names and Key Vault secret names.
NameSegment = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$", min_length=1, max_length=40)
]
# Key Vault secret names (upstream lower-cases them and converts '_' to '-').
SecretName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,126}$")]
# Model and alias names as used by the gateway (no whitespace or commas: they are joined into policies).
ModelName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")]
GuidStr = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")]
HttpsUrl = Annotated[str, StringConstraints(pattern=r"^https://[^\s]+$")]
Iso8601Duration = Annotated[str, AfterValidator(_check_duration)]


class CitadelModel(BaseModel):
    """Strict base model: camelCase aliases, unknown fields rejected."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        validate_assignment=True,
    )

    def to_param(self) -> Any:
        """Return the JSON-compatible representation used in rendered parameter files."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class UpstreamShapedModel(CitadelModel):
    """Model mirroring an upstream-owned object: unknown fields are preserved for forward compatibility."""

    model_config = ConfigDict(extra="allow")


class AzureResourceRef(CitadelModel):
    """Coordinates of an Azure resource such as an APIM instance or Key Vault."""

    subscription_id: GuidStr
    resource_group_name: str = Field(min_length=1, max_length=90)
    name: str = Field(min_length=1, max_length=260)


class FoundryProjectRef(CitadelModel):
    """Coordinates of a Microsoft Foundry project."""

    subscription_id: GuidStr
    resource_group_name: str = Field(min_length=1, max_length=90)
    account_name: str = Field(min_length=1)
    project_name: str = Field(min_length=1)


class StatusCodeRange(CitadelModel):
    min: int = Field(ge=100, le=599)
    max: int = Field(ge=100, le=599)

    @model_validator(mode="after")
    def _ordered(self) -> StatusCodeRange:
        if self.min > self.max:
            raise ValueError("min must be less than or equal to max")
        return self


class CircuitBreakerSettings(UpstreamShapedModel):
    """APIM native circuit-breaker settings (all optional; upstream defaults apply when omitted)."""

    enabled: bool | None = None
    failure_count: int | None = Field(default=None, ge=1)
    failure_interval: Iso8601Duration | None = None
    trip_duration: Iso8601Duration | None = None
    accept_retry_after: bool | None = None
    error_reasons: list[str] | None = None
    status_code_ranges: list[StatusCodeRange] | None = None


class ContractMetadata(CitadelModel):
    """Ownership and context for a contract. Owners drive "my contract" authorization."""

    owners: list[str] = Field(default_factory=list)
    owner_groups: list[str] = Field(default_factory=list)
    description: str = ""
    ticket: str | None = None

    @field_validator("owners", "owner_groups")
    @classmethod
    def _lower_unique(cls, values: list[str]) -> list[str]:
        seen: list[str] = []
        for value in values:
            normalized = value.strip().lower()
            if normalized and normalized not in seen:
                seen.append(normalized)
        return seen


def check_secret_ingress(model: BaseModel, context: str) -> None:
    """Reject literal secrets and redaction placeholders anywhere in a model."""
    ensure_no_secret_literals(
        model.model_dump(by_alias=True, exclude_none=True, mode="json"), context=context
    )


def check_deployment_name(name: str) -> None:
    """Raise when a nested ARM deployment name created by an upstream template would be too long."""
    if len(name) > ARM_DEPLOYMENT_NAME_LIMIT:
        raise ValueError(
            f"Generated deployment name '{name}' has {len(name)} characters; Azure allows "
            f"{ARM_DEPLOYMENT_NAME_LIMIT}. Shorten the business unit, use case or environment name."
        )
