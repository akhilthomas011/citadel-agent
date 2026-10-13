---
name: foundry-connection
description: Connect a Microsoft Foundry project to the gateway so its agents use governed models.
metadata:
  persona: any
  docs: "bicep/infra/foundry-integration/README.md#-overview bicep/infra/foundry-integration/README.md#-using-the-connection-in-agents bicep/infra/citadel-access-contracts/citadel-access-contracts-policy.md#foundry-projectmanagedidentity-jwt-validation-policy"
---

# Foundry Connection

## Overview

An access contract can create a connection in a Foundry project so the project's agents call models through the gateway with the contract's limits and policies.

## Interview

1. The Foundry project (subscription, resource group, account and project name).
2. Authentication: the project's managed identity (recommended, no stored key) or the subscription key.
3. Which models the agents need.

## Workflow

1. Start from the existing access contract with `describe_access_contract`, or create one with the access-contracts skill.
2. Call `draft_access_contract` with the Foundry connection enabled.
   * With the project managed identity, the tool adds the JWT settings Foundry tokens need (audience `https://cognitiveservices.azure.com` and the v1 issuer of the tenant).
   * With a subscription key, the key is stored in the connection itself.
3. Prepare, explain the what-if, get approval and execute as usual.
4. Explain how agents reference models through the connection: `<connection name>/<model>`. The connection name follows `Hub-<business unit>-<use case>-<ENV>-<code>`.

## Decision rules

* Prefer the managed identity: no key is stored in Foundry, and the gateway still checks the subscription key.
* Deploying the connection requires rights on the Foundry project; explain this when the change fails or is proposed as a pull request.

## Guardrails

* Do not mix the Foundry managed identity with custom JWT audience, issuer or role settings; the tool rejects that combination.
