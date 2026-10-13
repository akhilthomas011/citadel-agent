---
name: troubleshooting
description: Diagnose failed gateway calls (401, 403, 404, 429, 5xx) from the status, error body and request ID.
metadata:
  persona: any
  docs: "bicep/infra/llm-backend-onboarding/README.md#troubleshooting guides/entraid-auth-validation.md#troubleshooting-common-issues guides/llm-access-guide.md#retry-logic"
---

# Troubleshooting

## Overview

Use this skill when a call through the gateway fails or behaves unexpectedly.

## Workflow

1. Collect the HTTP status, the error body (with any key or token removed) and the gateway request ID: `activityId` in an error body, or the `UAIG-Request-Id` header when diagnostic headers are enabled. (`apim-request-id` comes from the model backend, not the gateway.) Ask for the use case and environment.
2. Call `explain_error` with the status, body and response header names. It matches the Citadel error catalog and suggests checks.
3. Run the suggested checks:
   * `describe_access_contract` for allowed models, limits, JWT and PII settings.
   * `list_models` when the model may be misspelled or not onboarded.
   * `smoke_test` to reproduce with the contract's own key.
   * `gateway_logs` with the request ID for 5xx errors and unexplained failures. Logs arrive two to five minutes after the call.
   * `usage_report` for throttling and quota questions.
4. Explain the cause in plain words, who can fix it (the caller, the platform team or an identity admin), and the exact next step. When the fix is a contract change, offer to prepare it.

## Decision rules

* 401 means the key or token is missing or invalid. 403 means the caller is known but not allowed: model not in the contract, missing app role, path not allowed, content safety, or an exhausted token quota.
* A 403 with a `Retry-After` header is an exhausted token quota, not an authorization problem.
* 429 is short-term throttling: honor `Retry-After`, then consider a higher tokens-per-minute limit or more backend capacity.
* A 500 with only "Internal server error" needs `gateway_logs`; the gateway log names the failing policy step.
* When the catalog has no match, say so, then search the documentation with `search_docs`.

## Guardrails

* Never ask users to paste keys or tokens. When a JWT must be inspected, ask them to decode it locally and share only the `aud`, `iss`, `roles` and `exp` claims.
* Do not change contracts while diagnosing; propose the change and use the normal approval flow.
