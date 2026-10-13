# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""The agent's own LLM client: Microsoft Agent Framework over the Citadel gateway (dogfooding).

The agent calls the gateway's OpenAI-compatible Unified AI surface with its own access-contract
key in the ``api-key`` header (the SDK's ``api_key`` holds a non-secret placeholder, the pattern
used by upstream's agent-framework validation notebook). Chat Completions is used rather than the
stateful Responses API because pooled backends without session affinity cannot serve follow-up
calls that reference a response ID created on another backend.
"""

from __future__ import annotations

from typing import Any

from azure.core.exceptions import ResourceNotFoundError

from citadel_agent.azure_clients.keyvault import KeyVaultAccessError, KeyVaultReader, vault_url
from citadel_agent.safety.redaction import default_redactor

UNIFIED_AI_V1 = "/unified-ai/v1"
# Not a secret: the SDK requires an api_key, while the real key travels in the api-key header.
SDK_PLACEHOLDER_KEY = "citadel-gateway"
APP_ID_HEADER = "x-app-id"
APP_ID = "citadel-agent"


class LlmConfigurationError(RuntimeError):
    """Raised when the agent's gateway access is not configured."""


def gateway_base_url(gateway_url: str) -> str:
    return gateway_url.rstrip("/") + UNIFIED_AI_V1


def resolve_agent_key(runtime: Any, environment: str | None = None) -> str:
    """Return the agent's gateway key (in memory only, registered for redaction).

    Development may supply ``CITADEL_AGENT_GATEWAY_KEY``; otherwise the key is read from the
    environment's Key Vault secret named in the admin policy (``agent.gatewayKeySecretName``).
    """
    if runtime.settings.gateway_key:
        key = runtime.settings.gateway_key
        default_redactor().register_secret(key)
        return key
    policy = runtime.policy
    if policy is None or policy.agent is None:
        raise LlmConfigurationError(
            "The admin policy has no 'agent' section; run 'citadel-agent bootstrap' first"
        )
    env = policy.environment(environment or policy.agent.environment)
    if env.key_vault is None:
        raise LlmConfigurationError("The agent environment has no Key Vault configured")
    reader = KeyVaultReader(runtime.credential, vault_url(env.key_vault.name))
    secret = policy.agent.gateway_key_secret_name.lower().replace("_", "-")
    try:
        return reader.get_secret_in_memory(secret)
    except ResourceNotFoundError as exc:
        raise LlmConfigurationError(
            f"Key Vault {env.key_vault.name} has no secret '{secret}'; run 'citadel-agent bootstrap' first"
        ) from exc
    except KeyVaultAccessError as exc:
        raise LlmConfigurationError(str(exc)) from exc


def create_chat_client(runtime: Any, *, model: str | None = None, environment: str | None = None) -> Any:
    """Create the Agent Framework chat client pointed at the Citadel gateway."""
    from agent_framework.openai import OpenAIChatCompletionClient

    policy = runtime.policy
    if policy is None or policy.agent is None:
        raise LlmConfigurationError(
            "The admin policy has no 'agent' section; run 'citadel-agent bootstrap' first"
        )
    env_name = environment or policy.agent.environment
    env = policy.environment(env_name)
    key = resolve_agent_key(runtime, env_name)
    return OpenAIChatCompletionClient(
        model=model or runtime.settings.model or policy.agent.model,
        api_key=SDK_PLACEHOLDER_KEY,
        base_url=gateway_base_url(env.gateway_url),
        default_headers={"api-key": key, APP_ID_HEADER: APP_ID},
    )
