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
2. Gather what to add or change: the model resource (Foundry, Azure OpenAI, or an external provider), the deployed model names, and the capacity or priority for load balancing.
3. Call `draft_backend_change`. It merges the change into the current contract without dropping existing backends and reports the effect on pools and aliases.
4. The gateway's managed identity needs an inference role on each new model resource. The plan includes this as a prerequisite step; explain who runs it.
5. Prepare, explain the what-if, get approval and execute.
6. Verify with `list_models` and a `smoke_test` from a contract that allows the model.

## Decision rules

* Use pools for the same model in several regions or resources. Prefer priority-based routing for provisioned throughput with pay-as-you-go spillover.
* Use aliases to give applications a stable name while backends change.
* Enable session affinity only for stateful models (Responses API, assistants), because it reduces load balancing.

## Guardrails

* Never remove a backend that access contracts still allow without confirming the impact with the user.
* External provider keys must be Key Vault references; never accept literal secrets.
