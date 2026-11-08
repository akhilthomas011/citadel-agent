---
name: backend-onboarding
description: Onboard model backends, pools and aliases through the backend contract (platform operators).
metadata:
  persona: operator
  docs: "bicep/infra/llm-backend-onboarding/README.md#configuration-reference bicep/infra/llm-backend-onboarding/README.md#circuit-breaker-configuration bicep/infra/llm-backend-onboarding/README.md#model-aliases bicep/infra/llm-backend-onboarding/README.md#session-affinity-configuration"
---

# Backend Onboarding

## Overview

The backend contract lists every model backend the gateway can route to. Deploying it replaces the whole configuration, so changes always start from the authoritative backend contract in the contracts repository and merge into it.

## Workflow

1. Call `backend_contract_summary` to show current backends, pools, aliases, circuit breaker and session affinity settings.
2. For Foundry or Azure OpenAI, identify the account and deployments. For
   another provider, gather its supported backend type, HTTPS endpoint, model
   IDs, authentication type, APIM named-value key and Key Vault secret URI.
3. Call `draft_backend_change`. It merges the change into the current contract without dropping existing backends and reports the effect on pools and aliases.
4. The gateway's managed identity needs an inference role on each new Azure model resource. The plan includes this as a prerequisite step; explain who runs it.
5. Prepare the change, explain the what-if, and get approval before execution.
6. Verify with `list_models` and a `smoke_test` from a contract that allows the model.

## Decision rules

* Use pools for the same model in several regions or resources. Prefer priority-based routing for provisioned throughput with pay-as-you-go spillover.
* Use aliases to give applications a stable name while backends change.
* Enable session affinity only for stateful models (Responses API, assistants), because it reduces load balancing.
* Use `aws-bedrock` for Bedrock's native Converse/InvokeModel surface with
  bearer API-key authentication. Use `aws-bedrock-mantle` for its
  OpenAI-compatible surface.
* Use `gemini` for Gemini's native API and `gemini-openai` for its
  OpenAI-compatible API. Use `anthropic` for Anthropic's Messages API.
* Use `external` for other OpenAI-compatible endpoints, with no authentication,
  bearer authentication, or an API key in the `api-key` header.
* Select the backend type that matches the provider's wire protocol. Aliases
  can combine models only when their backends use the same protocol.

## Guardrails

* Never remove a backend that access contracts still allow without confirming the impact with the user.
* Provider API keys must be Key Vault references with an APIM named-value key.
  Never ask for or accept a literal key.
* For bearer authentication, the Key Vault secret must contain the complete
  authorization header value expected by the upstream template. For
  `api-key-header`, the template sends the key in the `api-key` header.
  Gemini and Anthropic API-key auth use the raw key as the header value.
* Conversational Bedrock onboarding does not configure AWS SigV4 credentials
  because deployment-pipeline secret injection is not wired yet. Do not request
  AWS keys in chat.
* Check that APIM can reach external endpoints and that the provider account has
  the required model access. This tool does not probe external networks or
  provider quotas.
