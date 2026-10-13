---
title: Quickstart for application teams
description: Use Citadel Agent to find models, get working client code, onboard a use case and diagnose gateway errors without Azure API Management expertise
---

## What the agent does for you

Citadel Agent is a conversation in your terminal. You describe what you want, and the agent finds the answer in the Citadel documentation and in your live gateway, then proposes any change for your approval.

Typical requests from an application team:

* Which models can my team call, and with which limits?
* Give me working Python code for gpt-5.2.
* Onboard my chatbot with PII masking and a limit of 5,000 tokens per minute.
* Why do I get a 429 from the gateway?
* How many tokens did we use last week?
* Rotate our subscription key without downtime.

## Install and start

You need [uv](https://docs.astral.sh/uv/), Git and the Azure CLI. Sign in, install the dependencies and start a chat:

```bash
az login
git clone --recurse-submodules <this-repository-url>
cd AI-Gateway-Accelerator-Agent
uv sync --all-extras
uv run citadel-agent doctor
uv run citadel-agent chat
```

Your platform team supplies the contracts repository and environment settings. See the [operator guide](operator-guide.md) for how they are configured.

`chat` prints your identity, the persona derived from your Azure permissions, the environment and a session ID. Resume a conversation later with `--resume <session-id>`. Add `--environment test` to work in another environment. A line after each answer shows how many tokens the turn used.

## Ask questions

Start with read-only questions. They cannot change anything.

```text
Which models are available in dev?
Show me the HR chatbot contract.
Generate Python code for the Finance Forecast use case with gpt-5.2.
Smoke-test Finance Forecast.
Show usage for Finance Forecast for the last 7 days.
What does a 403 with unauthorized_model_access mean?
```

The generated code reads your subscription key from Key Vault at run time. The agent never prints a key.

Supported client code: OpenAI Python and JavaScript, Azure OpenAI Python, Microsoft Agent Framework, LangChain, Semantic Kernel, curl and PowerShell.

> [!TIP]
> Tools for drafting changes and diagnosing errors load when your message names the task. If the agent lacks a capability, say what you want to do, for example "onboard a use case", "rotate a key" or "why does this call fail".

## Change a contract

Describe the change in plain words. The agent asks for anything missing, then follows a fixed sequence:

1. It drafts the change and validates it against the admin policy limits.
2. It prepares the change: it generates the Citadel Bicep parameters, compiles them and runs an Azure what-if preview.
3. It explains the result in plain language and waits.
4. When you agree, it requests execution. A separate prompt shows the stored change, not the agent's description of it. Nothing happens until you approve.

What happens next depends on the environment mode:

| Mode     | Result of approval                                                        |
|----------|---------------------------------------------------------------------------|
| `direct` | The change deploys now and is recorded in a pull request for your records |
| `pr`     | A pull request opens, and the pipeline deploys after review and merge     |

Production always uses pull requests. If the admin policy refuses a change, the agent explains why and who can help. It does not look for a way around the policy.

## Rotate a key without downtime

Each API Management subscription has a primary and a secondary key, and the contract publishes one to Key Vault. Rotation moves clients to the other key before it replaces the old one:

1. Switch the published key and let every client read the new key.
2. The owners confirm that no client still uses the old key.
3. Regenerate the old key. The CLI asks you to type `ROTATE` for this irreversible step.
4. Turn rotation off again.

You can roll back before step 3. While a rotation is active, unrelated changes to that contract are blocked.

## Keep secrets out of the chat

Do not paste keys, tokens or connection strings. If you do, the agent does not repeat them and tells you to rotate the exposed key. Credentials live in Key Vault, and the agent gives you the secret name and code that reads it.

## Next steps

* [Troubleshooting](troubleshooting.md) for network, permission and package problems
* [Token usage](token-usage.md) to see what each conversation costs
* [Security model](security.md) for what the agent can and cannot do
