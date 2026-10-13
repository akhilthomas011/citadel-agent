---
name: operations-version
description: Check gateway versions, compatibility with the agent, environment health and contract inventory (platform operators).
metadata:
  persona: operator
  docs: "guides/release-version-management.md#version-tracks guides/release-version-management.md#runtime-version-endpoint bicep/infra/apim-gateway-upgrade/README.md#what-this-deployment-updates"
---

# Operations and Versions

## Overview

Use this skill for questions about the deployed gateway version, whether the agent can safely change it, upgrades, and the inventory of contracts.

## Workflow

1. Call `gateway_overview` for the release tracks of the live gateway, the agent's tested versions and the compatibility verdict.
2. Call `preflight` when changes fail or the user asks what they can do: it reports roles per capability, network reachability of Key Vault, Cosmos DB and Log Analytics, and the admin policy mode.
3. Call `list_access_contracts` for an inventory of use cases with their models and limits, from the contracts repository and the live gateway.
4. Call `backend_contract_summary` for backends, pools and aliases.

## Decision rules

* When the gateway is newer or older than the agent's tested versions, the agent stays read-only for affected contract types. Explain the mismatch and that the platform team upgrades the agent or the gateway.
* Gateway upgrades use the upstream upgrade template, not a redeployment of the original infrastructure. The agent explains the process but does not run upgrades.

## Guardrails

* Do not describe a partial compatibility as safe; quote the verdict from `gateway_overview`.
