---
name: access-contracts
description: Onboard a use case or change its access contract (models, token limits, JWT, PII, content safety) through an interview.
metadata:
  persona: any
  docs: "bicep/infra/citadel-access-contracts/README.md#-quick-start-guide bicep/infra/citadel-access-contracts/citadel-access-contracts-policy.md#available-policies-snippets bicep/infra/citadel-access-contracts/citadel-access-contracts-policy.md#model-capacity-management-policy"
---

# Access Contracts

## Overview

An access contract gives one use case its own APIM product, a subscription key in Key Vault and a product policy. Use this skill to onboard a new use case or change an existing one.

## Interview

Ask only what you cannot infer, a few questions at a time:

1. Business unit and use case name (short, no spaces), and the environment (dev, test or prod).
2. Owners: who may change the contract later (the caller is added by default).
3. Models the use case needs. Check them with `list_models`.
4. Expected volume, to set tokens per minute and an optional monthly token quota. Stay within the admin caps; the tool reports violations.
5. Data sensitivity: PII anonymization or blocking, and content safety.
6. Authentication: subscription key only, or also an Entra ID JWT (with optional app roles).
7. Whether a Foundry project should get a gateway connection (see the foundry-connection skill).

## Workflow

1. For a change, start from the current contract with `describe_access_contract`. Never rebuild an existing contract from scratch.
2. Call `draft_access_contract` with the answers. Fix any validation errors it returns by asking the user, not by guessing.
3. Summarize the draft in plain language: product name, models, limits, safety settings and the Key Vault secret names.
4. Call `prepare_change`. Explain the what-if result: new resources, modified ones, and changes marked expected (known what-if noise).
5. Ask for approval, then call `execute_change` with the change ID and approval hash.
6. In direct mode, confirm the deployment and offer `smoke_test` and `generate_client_code`. In pull request mode, share the link and explain that the pipeline deploys after review; check later with `change_status`.

## Decision rules

* The product name is `<code>-<business unit>-<use case>-<ENV>`. Changing the service code, business unit or use case creates a new product and a new key, so warn before renaming.
* Allowed models are a comma-separated list without spaces in the policy; the tool handles formatting.
* Prefer PII anonymization for chat assistants and blocking for strict compliance cases. With anonymization the model sees placeholders such as `<Email_0>`; tell the team to instruct their model to keep placeholders unchanged so the gateway can restore the original values in the response.
* Use per-model token limits only when the user asks for different limits per model.

## Guardrails

* Respect the admin policy. Do not try to work around caps, ownership or mode; explain the limit and who can change it.
* Never request or display key values. Point to the Key Vault secret names instead.
