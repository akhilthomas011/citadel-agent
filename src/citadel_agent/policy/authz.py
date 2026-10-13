# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Authorization: derived personas, allowed operations, ownership and caps.

In local mode these checks are guardrails (the user runs the agent with their own credentials).
The security boundaries remain Azure RBAC, Key Vault/Cosmos data-plane RBAC, branch protection and
pipeline environment approvals. The checks are still re-evaluated inside every tool and executor so
the agent never relies on tool filtering alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from citadel_agent.azure_clients.arm import PermissionSet
from citadel_agent.azure_clients.credentials import PrincipalInfo
from citadel_agent.contracts.access import AccessContractIntent, ProductPolicyIntent, TokenLimit
from citadel_agent.contracts.intent_io import ContractIntent
from citadel_agent.policy.admin import (
    WRITE_OPERATIONS,
    AdminPolicy,
    Caps,
    ChangeMode,
    LoadedPolicy,
    Operation,
)

APIM_OPERATOR_ACTIONS: tuple[str, ...] = (
    "Microsoft.ApiManagement/service/backends/write",
    "Microsoft.ApiManagement/service/write",
)
_MONTHLY_FACTOR = {"Hourly": 730.0, "Daily": 30.0, "Weekly": 4.345, "Monthly": 1.0, "Yearly": 1 / 12}


class Persona(StrEnum):
    CONSUMER = "consumer"
    OPERATOR = "operator"


class AuthorizationError(PermissionError):
    """Raised when the caller may not perform an operation."""


@dataclass(frozen=True)
class Caller:
    """The signed-in principal and the persona derived for it."""

    principal: PrincipalInfo
    persona: Persona
    group_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def identifiers(self) -> set[str]:
        values = {self.principal.object_id, self.principal.display_name}
        return {v.lower() for v in values if v}


def derive_persona(
    policy: AdminPolicy | None,
    permissions: PermissionSet | None,
    group_ids: Iterable[str] = (),
) -> Persona:
    """Derive the persona from Azure RBAC on the APIM instance and admin-defined groups."""
    if policy is None:
        return Persona.CONSUMER
    operator_groups = {g.lower() for g in policy.personas.operator_group_object_ids}
    if operator_groups and operator_groups & {g.lower() for g in group_ids}:
        return Persona.OPERATOR
    if (
        policy.personas.operator_requires_apim_write
        and permissions is not None
        and any(permissions.allows(action) for action in APIM_OPERATOR_ACTIONS)
    ):
        return Persona.OPERATOR
    return Persona.CONSUMER


def monthly_equivalent(limit: TokenLimit) -> float | None:
    """Convert a token quota to its approximate monthly equivalent for cap comparison."""
    if limit.token_quota is None or limit.token_quota_period is None:
        return None
    return limit.token_quota * _MONTHLY_FACTOR[limit.token_quota_period]


def _policy_cap_violations(code: str, policy: ProductPolicyIntent, caps: Caps) -> list[str]:
    problems: list[str] = []
    limits = [policy.token_limit, *policy.model_token_limits]
    for limit in (item for item in limits if item is not None):
        if caps.max_tokens_per_minute and limit.tokens_per_minute > caps.max_tokens_per_minute:
            problems.append(
                f"{code}: {limit.tokens_per_minute} tokens/minute exceeds the cap of "
                f"{caps.max_tokens_per_minute}"
            )
        monthly = monthly_equivalent(limit)
        if caps.max_monthly_token_quota and monthly and monthly > caps.max_monthly_token_quota:
            problems.append(
                f"{code}: token quota (~{monthly:,.0f}/month) exceeds the cap of "
                f"{caps.max_monthly_token_quota:,}"
            )
    if caps.max_tokens_per_minute and policy.token_limit is None and not policy.model_token_limits:
        problems.append(f"{code}: a token limit is required in this environment")
    if caps.allowed_models:
        permitted = {m.lower() for m in caps.allowed_models}
        if not policy.allowed_models:
            problems.append(f"{code}: allowedModels must be listed explicitly in this environment")
        disallowed = [m for m in policy.allowed_models if m.lower() not in permitted]
        if disallowed:
            problems.append(f"{code}: models not permitted in this environment: {', '.join(disallowed)}")
    if policy.response_headers and not caps.allow_response_headers:
        problems.append(f"{code}: debug response headers are not allowed in this environment")
    return problems


def cap_violations(intent: ContractIntent, caps: Caps) -> list[str]:
    """Return human-readable cap violations for an intent (empty when compliant)."""
    if not isinstance(intent, AccessContractIntent):
        return []
    problems: list[str] = []
    if not intent.store_credentials_in_key_vault and not caps.allow_direct_output_credentials:
        problems.append(
            "Keys must be stored in Key Vault in this environment (direct-output credentials are off)"
        )
    for service in intent.services:
        if caps.allowed_service_codes and service.code not in caps.allowed_service_codes:
            problems.append(f"Service code {service.code} is not allowed in this environment")
        # Token and model caps govern LLM traffic; a service that only grants tools or agents has none.
        if service.llm_apis or not service.is_multi_asset:
            problems.extend(_policy_cap_violations(service.code, service.policy, caps))
    return problems


class Authorizer:
    """Answers "may this caller do X here?" from the loaded admin policy."""

    def __init__(self, loaded: LoadedPolicy, caller: Caller) -> None:
        self.loaded = loaded
        self.caller = caller

    @property
    def policy(self) -> AdminPolicy | None:
        return self.loaded.policy

    def allowed_operations(self) -> set[str]:
        if self.policy is None:
            # Fail closed: read-only operations only.
            return {op.value for op in Operation if op not in WRITE_OPERATIONS}
        listed = (
            self.policy.operations.operator
            if self.caller.persona is Persona.OPERATOR
            else self.policy.operations.consumer
        )
        if "*" in listed:
            return {op.value for op in Operation}
        return set(listed)

    def can(self, operation: Operation) -> bool:
        return operation.value in self.allowed_operations()

    def require(self, operation: Operation) -> None:
        if operation in WRITE_OPERATIONS and not self.loaded.writable:
            reason = self.loaded.error or "no admin policy is loaded"
            raise AuthorizationError(
                f"Changes are disabled because the admin policy is unavailable: {reason}"
            )
        if not self.can(operation):
            raise AuthorizationError(
                f"The {self.caller.persona.value} persona is not allowed to perform '{operation.value}'."
            )

    def owns(self, intent: ContractIntent) -> bool:
        """Operators own everything; consumers own contracts that list them as owners."""
        if self.caller.persona is Persona.OPERATOR:
            return True
        return bool(self.caller.identifiers & set(intent.metadata.owners))

    def require_owner(self, intent: ContractIntent) -> None:
        if not self.owns(intent):
            raise AuthorizationError("You are not an owner of this contract (metadata.owners).")

    def change_mode(self, environment: str) -> ChangeMode:
        if self.policy is None:
            raise AuthorizationError("No admin policy is loaded")
        return self.policy.environment(environment).mode

    def check_caps(self, intent: ContractIntent) -> list[str]:
        if self.policy is None:
            return ["No admin policy is loaded"]
        return cap_violations(intent, self.policy.environment(intent.environment).caps)
