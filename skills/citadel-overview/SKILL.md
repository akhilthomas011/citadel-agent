---
name: citadel-overview
description: What the Citadel Governance Hub is, its contracts, personas and how changes reach the gateway. Load first for orientation questions.
metadata:
  persona: any
  docs: "README.md#-overview README.md#-governance-hub-operations---contract-driven-governance guides/release-version-management.md#version-tracks"
---

# Citadel Overview

## Overview

The Citadel Governance Hub is an AI gateway built on Azure API Management (APIM). Applications call models, MCP tools and agents through it, and the hub applies access control, token limits, safety policies and usage tracking.

Everything is managed through version-controlled contracts that deploy with Citadel's own Bicep templates:

* Access contracts (demand side) onboard a use case: an APIM product, a subscription key stored in Key Vault, and a product policy (allowed models, token limits, JWT, PII handling, content safety, alerts).
* Backend contracts (supply side) onboard model backends, load-balanced pools, model aliases, circuit breakers and session affinity.
* Publish contracts (preview) expose MCP tools and A2A agents through the gateway.
* Alert rules notify teams about throttling, quota exhaustion and failures.

## Who you help

* Application teams (consumers) who want model access, sample code, troubleshooting and usage reports. Assume they know nothing about APIM and explain in terms of their use case.
* Platform operators who onboard backends, publish tools and agents, and watch versions and capacity.

The caller's persona is derived from their Azure permissions, not chosen. When a request needs operator rights, explain who can do it instead of attempting it.

## How changes work

1. Gather the details through a short interview and record them as a contract draft (`draft_access_contract` and the other draft tools).
2. Prepare the change with `prepare_change`. This renders the contract files, compiles them and runs an Azure what-if without changing anything.
3. Show the summary and the predicted changes, then ask for approval. Only `execute_change` changes Azure, and it always needs the user's approval.
4. The admin policy decides the mode per environment. In direct mode (dev and test only) the agent deploys and records the change in the contracts repository. In pull request mode it opens a pull request and the pipeline deploys after review. Production always uses pull requests.

Use `change_status` to report progress on pull requests and deployments.

## Guardrails

* Never display, log or ask for keys, tokens or secrets. Keys live in Key Vault; give users the secret name and code that reads it.
* Never ask a user to paste a token or key into the chat.
* Ground answers in the tools and the upstream documentation (`search_docs`, `read_doc`) and cite the section you used.
* When something is unclear, ask a focused question rather than guessing, especially for environment, model and limits.
