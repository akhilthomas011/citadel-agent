---
name: publish-contracts
description: Publish an MCP server or A2A agent through the gateway, then grant use cases access to it.
metadata:
  persona: operator
  docs: "bicep/infra/citadel-publish-contracts/publish-contract-guide.md#1-concepts bicep/infra/citadel-publish-contracts/publish-contract-guide.md#2-parameter-schema bicep/infra/citadel-publish-contracts/publish-contract-guide.md#a2a-authentication-model-important bicep/infra/citadel-publish-contracts/publish-contract-guide.md#8-publishing-a-foundry-agent-as-a2a"
---

# Publish Contracts

## Overview

Publish contracts (preview) put MCP servers and A2A agents behind the gateway, with rate limits, usage tracking and optional API Center registration. Consumers then get access through an access contract that lists the published API.

## Interview

1. Asset type: MCP server (existing remote MCP endpoint) or A2A agent (including a Foundry agent).
2. Name, environment and the backend URL.
3. How the gateway authenticates to the backend: none, a key from Key Vault, or the gateway's managed identity.
4. Which use cases should get access, and with what call limits.

## Workflow

1. Call `draft_publish_contract` with the answers and fix validation errors with the user.
2. Call `draft_access_contract` for each use case that needs access, listing the published API as a tool or agent API.
3. Call `prepare_change` with all drafts. The publish step runs first; the access steps depend on it, so their what-if is deferred until the API exists.
4. Explain any manual prerequisites in the plan (for example Foundry agent settings or role assignments). They are run by an authorized person unless the admin policy allows the agent to run them.
5. Get approval and call `execute_change`.

## Decision rules

* MCP responses stream, so policies must not read the response body; the generated access policy already accounts for this.
* A Foundry agent published as A2A needs the gateway's identity to hold an agent-consumer role on the Foundry project.

## Guardrails

* Publish contracts are a preview feature. Mention this and keep changes in dev or test until validated.
* Backend credentials must come from Key Vault references; never accept a literal secret.
