---
title: Publishing tools and agents
description: Publish an MCP server or an A2A agent through the Citadel gateway, then grant use cases access to it, using publish contracts
---

## Overview

A publish contract puts an MCP server or an A2A agent behind the Citadel gateway, with rate limits, usage tracking and optional API Center registration. Use cases then get access through an access contract that lists the published API.

> [!WARNING]
> Publish contracts are a preview feature of Citadel. The agent states this on every result and requires the live gateway's `publish-contract-version` to match the version it was tested with. Keep changes in development or test until you have validated them.

Publishing is an operator task. A consumer who asks is told to involve the platform team, and the authorization is checked again inside the tool.

## Asset types

| Type            | What it publishes                                                | Key inputs                                  |
|-----------------|------------------------------------------------------------------|---------------------------------------------|
| `mcp-from-api`  | An MCP tool built from an API that already exists in the gateway | `source_api`, `operations`                  |
| `mcp-existing`  | A remote MCP server                                              | `backend_url`, `transport`                  |
| `a2a`           | A generic A2A agent                                              | `backend_url`, `agent_id`, `agent_card_url` |
| `foundry-agent` | A Foundry-hosted agent exposed as A2A                            | `foundry_project_id`, `foundry_agent`       |

For `mcp-existing` and `a2a`, the gateway authenticates to the backend with `auth_type`, `auth_resource`, `auth_header_name` and a `key_vault_secret_uri`. Only Key Vault secret URIs are accepted as credentials. A literal key, a URL with credentials or a URL with a query string is rejected, and the error names the parameter but never repeats the value.

## Ask for it

Tell the agent what you want in plain words:

```text
Publish our remote MCP server https://tools.example.com/mcp as weather-tools in dev,
then give the HR ChatBot use case access to it. Prepare the change, do not deploy.
```

The agent drafts one asset per call. Repeat with the same contract name, or pass a draft ID, to add more assets to the same contract. It rejects a name or gateway path that another publish contract or a pending draft already uses, and a name that clashes with a built-in LLM API.

## Publish, then grant access

Access depends on the published API existing. When a change includes both, the agent orders the steps:

1. The publish step deploys first.
2. The access step depends on it, so its what-if preview is deferred until the API exists.

An access contract can grant tools and agents with `tool_apis`, `agent_apis` and a required `asset_calls_per_minute`. The grant creates a separate `MULTI` service in the contract. The existing model service and its key stay unchanged.

> [!NOTE]
> Token and model caps in the admin policy apply to services that grant LLM APIs. A service that only grants tools or agents has no token limit to cap, so those caps skip it. All other caps still apply.

Granting an API that the repository and pending drafts do not know about produces a warning instead of an error, because it may have been published outside the agent.

## Foundry agents

Publishing a Foundry agent as A2A needs two things that Citadel's contracts do not cover. The agent records them as manual prerequisite steps with exact commands, and an authorized person runs them:

* Grant the gateway's managed identity the Foundry Agent Consumer role on the project. The step is skipped when ARM shows the identity already holds that role or Azure AI User.
* Patch the Foundry agent to enable A2A.

The agent never runs these itself. See the [security model](security.md).

## After the change

Ask the agent to run a smoke test against the new API, or check the change status. In pull request mode the status includes the pull request, its checks and the Azure deployment state.
