---
name: calling-models
description: Find available models and produce working client code (OpenAI, Azure OpenAI, Agent Framework, LangChain, curl) for the gateway.
metadata:
  persona: any
  docs: "guides/llm-access-guide.md#the-three-llm-apis guides/llm-access-guide.md#when-to-use-what guides/llm-access-guide.md#unified-ai-deployment-discovery"
---

# Calling Models

## Overview

Use this skill when someone asks which models they can use, how to call the gateway, or wants sample code.

## Prerequisites

* The caller needs an access contract for the environment. If they have none, switch to the access-contracts skill.
* Code reads the subscription key from the Key Vault secret named in the contract; the agent never prints the key.

## Workflow

1. Identify the use case and environment. Call `describe_access_contract` to get the allowed models, the key's Key Vault secret name and whether JWT is required.
2. Call `list_models` to show the models the contract can use. Explain aliases in plain words when the gateway uses them.
3. Pick the API surface:
   * Unified AI (`/unified-ai/v1`) is the default. It is OpenAI-compatible and the model goes in the request body.
   * The Azure OpenAI surface (`/openai/deployments/<model>`) suits existing Azure OpenAI SDK code.
   * Native Bedrock, Gemini or Claude paths are needed only for models whose backend requires them.
4. Call `generate_client_code` with the framework, language, model and surface. Present the code and point out where the key comes from.
5. Offer `smoke_test` to prove access works before the team writes code.

## Decision rules

* Send the subscription key in the `api-key` header. OpenAI SDKs still require an API key argument, so the generated code passes a placeholder there and sends the real key as a default header.
* Recommend Chat Completions for multi-turn conversations. The stateful Responses API can fail on pooled backends without session affinity.
* List models with `GET /unified-ai/deployments`. The OpenAI SDK's `models.list()` does not work through the gateway at this version.
* Suggest the `x-app-id` header so usage reports can attribute calls to each application.
* When JWT is required, the code also acquires an Entra ID token for the gateway's app registration and sends both credentials.

## Guardrails

* Never place a key literal in generated code; always read it from Key Vault or an environment variable.
* Do not recommend models outside the contract's allowed models; offer a contract change instead.
