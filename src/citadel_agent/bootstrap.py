# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Bootstrap the agent's own access contract so its LLM calls go through the Citadel gateway."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from citadel_agent.changes.engine import ChangeRequest, IntentChange
from citadel_agent.contracts.access import AccessContractIntent
from citadel_agent.policy.admin import AdminPolicy, Operation

AGENT_BUSINESS_UNIT = "Platform"
AGENT_USE_CASE = "CitadelAgent"
AGENT_ENDPOINT_SECRET = "CITADEL-AGENT-LLM-ENDPOINT"  # noqa: S105 - a Key Vault secret name, not a secret
AGENT_APIS = ["unified-ai-api", "universal-llm-api"]
DEFAULT_AGENT_TPM = 20000
DEFAULT_AGENT_MONTHLY_QUOTA = 20_000_000


class BootstrapError(RuntimeError):
    """Raised when the bootstrap cannot run."""


def agent_access_intent(
    policy: AdminPolicy, owner: str, environment: str | None = None
) -> AccessContractIntent:
    """The access contract for the agent's own model access (product LLM-Platform-CitadelAgent-<ENV>)."""
    if policy.agent is None:
        raise BootstrapError(
            "The admin policy has no 'agent' section (environment, model, gatewayKeySecretName)"
        )
    env_name = environment or policy.agent.environment
    env = policy.environment(env_name)
    caps = env.caps
    tpm = min(DEFAULT_AGENT_TPM, caps.max_tokens_per_minute or DEFAULT_AGENT_TPM)
    quota = min(DEFAULT_AGENT_MONTHLY_QUOTA, caps.max_monthly_token_quota or DEFAULT_AGENT_MONTHLY_QUOTA)
    return AccessContractIntent.model_validate(
        {
            "environment": env_name,
            "metadata": {
                "owners": [owner],
                "description": "Citadel Agent: the agent's own LLM access through the gateway (dogfooding).",
            },
            "useCase": {
                "businessUnit": AGENT_BUSINESS_UNIT,
                "useCaseName": AGENT_USE_CASE,
                "environment": env_name.upper(),
            },
            "services": [
                {
                    "code": "LLM",
                    "apis": AGENT_APIS,
                    "keySecretName": policy.agent.gateway_key_secret_name,
                    "endpointSecretName": AGENT_ENDPOINT_SECRET,
                    "policy": {
                        "allowedModels": [policy.agent.model],
                        "tokenLimit": {
                            "tokensPerMinute": tpm,
                            "tokenQuota": quota,
                            "tokenQuotaPeriod": "Monthly",
                        },
                    },
                }
            ],
        }
    )


def bootstrap_request(policy: AdminPolicy, owner: str, environment: str | None = None) -> ChangeRequest:
    intent = agent_access_intent(policy, owner, environment)
    return ChangeRequest(
        summary="Bootstrap the Citadel Agent's own access contract",
        environment=intent.environment,
        changes=(IntentChange("agent-access", intent, Operation.ACCESS_WRITE),),
    )


def run_bootstrap(
    runtime: Any, *, confirm: Callable[[str], bool], environment: str | None = None
) -> dict[str, Any]:
    """Prepare, confirm and execute the agent's access contract. Returns the execution result."""
    policy = runtime.policy
    if policy is None:
        raise BootstrapError(runtime.loaded.error or "No admin policy is loaded")
    engine = runtime.engine()
    prepared = engine.prepare(bootstrap_request(policy, runtime.principal.label, environment))
    if not confirm(prepared.summary()):
        return {"change": prepared.change.id, "state": "not-approved"}
    return engine.execute(prepared.change.id, prepared.content_hash)
