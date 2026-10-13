---
title: Troubleshooting
description: Fix common problems when running Citadel Agent locally, from network rules and permissions to package proxies and what-if noise
---

## Run the diagnostics first

```bash
uv run citadel-agent doctor
```

`doctor` shows who you are, which persona applies, whether the admin policy loaded, whether the gateway version is compatible with the agent, and whether you can deploy directly. In chat, ask the agent to run a preflight check for a fuller list of what you can reach.

## Network rules or permissions

Citadel's Key Vault, Cosmos DB and Foundry accounts are private by default, so a developer machine usually fails on the network before permissions matter. The two problems look alike (a firewall block is also an HTTP 403) and need different fixes. The agent classifies every failure:

| Status         | Meaning                                                 | Fix                                                                                        |
|----------------|---------------------------------------------------------|--------------------------------------------------------------------------------------------|
| `blocked`      | The resource's network rules reject your address        | Ask the platform team to allow your address (development only), or use the VPN or jump box |
| `missing role` | You reach the resource, but your account may not use it | Ask for the role the message names                                                         |
| `unreachable`  | The name does not resolve or the connection times out   | The resource is only reachable inside the hub network                                      |

Your public address changes when you change networks. When the error names it, send that exact address to the platform team.

> [!NOTE]
> When Cosmos DB is blocked, usage reports fall back to Application Insights token metrics. They show usage but no cost, and the report says why.

## The agent cannot reach its model

```bash
uv run citadel-agent llm-check
```

The check performs a tool call and a streamed answer through the gateway. If it fails:

* A `403` with `unauthorized_model_access` means the agent's own contract does not list the model. Change the model with `CITADEL_AGENT_MODEL` or update the contract.
* An error about the key means `CITADEL_AGENT_GATEWAY_KEY` is unset and the Key Vault secret `citadel-agent-llm-key` cannot be read. See the previous section.

## Packages do not install on a corporate network

If your network reaches PyPI through a proxy, put the index in a local `uv.toml`, which is ignored by git:

```toml
[[index]]
url = "https://<your-package-proxy>/pypi/simple/"
default = true
```

`uv tool install` ignores the project's `uv.toml`. Pass the index explicitly:

```bash
uv tool install --default-index <proxy-url> git+https://github.com/<org>/<agent-repo>@<commit>
```

Do not commit `uv.lock` if it records the proxy address.

## What-if shows changes that are not real

Azure API Management reports some resources as changed on every deployment. The agent labels these as expected and explains why:

* Product-to-API links always show as `Create`.
* Policies deployed as raw XML always show as `Modify`, because API Management stores its own format.
* Backend pool members compare short and full resource IDs.

A policy change counts as real when the generated policy file itself changed. Compare the file in the pull request.

## Windows specifics

* Azure CLI output with `--stdout` can fail on non-ASCII characters. The agent writes to files instead.
* `uv run` rebuilds the project when `pyproject.toml` changes and fails if another process holds `citadel-agent.exe`. Use `uv run --no-sync` while another agent session is running.
* Bicep needs forward slashes in relative paths. The agent normalizes them.

## Changes stay prepared

Prepared changes live in `.citadel-agent/changes/`. A change expires in practice when the repository, admin policy, upstream pin or the live gateway version moves, because execution re-checks all of them. Prepare the change again. Changes prepared before the template-hash change in this release also need to be prepared again.
