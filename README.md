---
title: Citadel Agent
description: AI assistant that helps application teams and platform operators use and operate the Citadel Governance Hub AI gateway through its contract-driven Bicep framework
---

## Overview

Citadel Agent is a Python application built on Microsoft Agent Framework. It helps people with little Azure API Management experience use the [Citadel Governance Hub](https://github.com/Azure-Samples/ai-hub-gateway-solution-accelerator/tree/citadel-v1) for their AI workloads, and helps platform teams operate it.

Citadel manages the gateway through version-controlled contracts (`.bicepparam` files):

* Backend contracts onboard LLM backends and models
* Access contracts onboard use cases with products, subscriptions and policies
* Publish contracts expose MCP tools and A2A agents

The agent always deploys through these upstream contracts. It never re-implements the infrastructure as code.

### Release scope

The v1.0 target is operations for an **existing hub**, not provisioning the
entire accelerator or supporting every upstream API. See the
[release notes](CHANGELOG.md) and [security policy](SECURITY.md).
Container Apps hosting with a UI is planned separately; DevUI is for local
testing, not a production multi-user frontend. Production deployments require
enforced repository reviews, environment approvals and Azure identity controls.

The agent is [MIT licensed](LICENSE). Existing source and upstream copyright
and license notices remain in force.

## What it can do

Application teams can:

* Discover models and limits, and generate client code for OpenAI, Azure OpenAI, Agent Framework, LangChain, Semantic Kernel, curl and PowerShell
* Onboard a use case or change its contract, with a what-if preview before anything deploys
* Diagnose gateway errors, read gateway logs, smoke-test access and report usage and estimated cost
* Rotate subscription keys without downtime and create alerts for a use case

Platform operators can also:

* Onboard model backends, discover deployments and manage model aliases, the circuit breaker and session affinity
* Publish MCP tools and A2A agents (preview), then grant use cases access to them
* Create gateway-wide alerts, check versions against the agent's tested range and keep the agent in step with upstream

Changes go through a prepare and approve flow in either direct mode (development and test) or pull request mode (everywhere, and mandatory for production). See the [documentation](#documentation).

## Prerequisites

* [uv](https://docs.astral.sh/uv/) (Python 3.11 is downloaded automatically)
* Git
* Azure CLI with Bicep (`az bicep install`) for contract validation and deployments
* Azure Developer CLI (`azd`) for deploying a Citadel environment

## Getting started

Clone the repository with its pinned upstream submodule and install the dependencies:

```bash
git clone --recurse-submodules <this-repository-url>
cd AI-Gateway-Accelerator-Agent
uv sync --locked --all-extras
```

> [!TIP]
> If your network reaches PyPI through a package proxy, create a local `uv.toml` (ignored by git) in the repository root:
>
> ```toml
> [[index]]
> url = "https://<your-package-proxy>/pypi/simple/"
> default = true
> ```

Check the installation:

```bash
uv run citadel-agent version
uv run citadel-agent upstream status
```

### Installed package (no checkout at runtime)

Wheels include trusted skills, repository templates, example configuration and
runtime content from the exact upstream pin, including its license. They do not
fetch a floating upstream branch at startup. Upstream synchronization is a
maintainer operation requiring a source checkout; update the installed artifact
to consume a newer tested pin.

To build and install a reproducible artifact on Windows:

```powershell
uv sync --locked --all-extras --group build --no-install-project --no-build
uv sync --locked --all-extras --group build --no-build-isolation
uv build --no-build-isolation --python .venv\Scripts\python.exe --out-dir build\dist
uv export --locked --all-extras --no-dev --no-emit-project --output-file build\runtime-requirements.txt
uv venv build\installed --python 3.11
uv pip install --python build\installed\Scripts\python.exe --require-hashes -r build\runtime-requirements.txt
$wheel = (Get-ChildItem build\dist\*.whl).FullName
uv pip install --python build\installed\Scripts\python.exe --no-deps $wheel
```

On Linux, virtualenv Python is under `bin/python` rather than `Scripts/python.exe`.
Use the same lock for dependency installation; installing only the wheel with
unconstrained dependency resolution is not the reproducible release path.

The installed CLI works from any directory. Configuration comes from environment
variables or the current directory's `.env`. Set `CITADEL_AGENT_STATE_DIR` to a
writable persistent location; packaged defaults are `%LOCALAPPDATA%\citadel-agent`
on Windows and `$XDG_STATE_HOME/citadel-agent` (or `~/.local/state/citadel-agent`)
on Linux. State must not be written into the installed package. This enables a
future container deployment, but persistence, user isolation and UI design remain
separate work. `repo init` still needs Git/network access to clone the exact
upstream commit into the new contracts repository.

## Set up an environment

The agent works with an existing Citadel hub and a canonical contracts repository. The repository holds the contracts and the admin policy that decides, per environment, whether changes deploy directly (dev and test only) or through pull requests.

1. Sign in with `az login`.
2. Describe the hub for the admin policy. The command prints an `environments` entry built from the resources in the hub's resource group:

   ```bash
   uv run citadel-agent env discover --subscription <subscription-id> --resource-group <hub-resource-group>
   ```

3. Copy `config/admin-policy.example.yaml`, then paste the discovered environment into it.
4. Create the canonical contracts repository. It is pinned to the same upstream commit as the agent:

   ```bash
   uv run citadel-agent repo init --path ../citadel-contracts --policy admin-policy.yaml
   ```

5. Push the repository to your git host, then point the agent at it in a `.env` file (ignored by git):

   ```text
   CITADEL_AGENT_REPO_URL=https://github.com/<org>/citadel-contracts.git
   ```

   For local-only development, set `CITADEL_AGENT_REPO_URL` to the repository's folder instead.
6. Check the setup. `doctor` reports your derived persona, the policy source, gateway compatibility and whether you can deploy directly:

   ```bash
   uv run citadel-agent doctor
   ```

7. Create the agent's own access contract, so the agent calls its model through the gateway, then verify tool calling and streaming:

   ```bash
   uv run citadel-agent bootstrap
   uv run citadel-agent llm-check
   ```

8. Start a conversation, or open the Agent Framework DevUI in a browser:

   ```bash
   uv run citadel-agent chat
   uv run citadel-agent devui
   ```

   `chat` keeps conversations under `.citadel-agent/sessions`; continue one with `--resume <session-id>`. Changes always need your approval: the prompt shows the stored change set, not the model's description of it. `devui` prints an access token that the DevUI asks for, because the agent can deploy changes.

The agent never edits your working copy of the contracts repository. It keeps its own clone under `.citadel-agent/repo` and records every change there first:

* For a hosted repository, the agent opens a pull request. Changes already deployed directly carry the `deployed-directly` label.
* For a local repository, a directly deployed change fast-forwards its `main` branch. `repo init` configures the repository to accept this. Git refuses the update while the repository has uncommitted changes, and the agent then reports where the change is.

### Settings

| Variable                         | Purpose                                                                  |
|----------------------------------|--------------------------------------------------------------------------|
| `CITADEL_AGENT_REPO_URL`         | Canonical contracts repository: git URL, or a local path for development |
| `CITADEL_AGENT_WORKSPACE_DIR`    | The agent's working clone (default `.citadel-agent/repo`)                |
| `CITADEL_AGENT_BASE_BRANCH`      | Protected base branch of the repository (default `main`)                 |
| `CITADEL_AGENT_ENVIRONMENT`      | Environment to work with (default: the policy's `defaultEnvironment`)    |
| `CITADEL_AGENT_MODEL`            | Override the model the agent uses (default: the policy's `agent.model`)  |
| `CITADEL_AGENT_GATEWAY_KEY`      | Development only: the agent's gateway key instead of Key Vault           |
| `CITADEL_AGENT_BOOTSTRAP_POLICY` | Development only: a local admin policy for dev-class environments        |
| `CITADEL_AGENT_OTEL_ENDPOINT`    | OTLP endpoint for OpenTelemetry traces (prompts are never exported)      |
| `CITADEL_AGENT_OTEL_CONSOLE`     | `true` prints OpenTelemetry spans to the console                         |
| `GITHUB_TOKEN`                   | Token for opening pull requests on GitHub                                |

### Commands

| Command                            | Purpose                                                           |
|------------------------------------|-------------------------------------------------------------------|
| `citadel-agent env discover`       | Print an admin policy environment entry for an existing hub       |
| `citadel-agent repo init`          | Create a canonical contracts repository from the template         |
| `citadel-agent doctor`             | Diagnose identity, policy, gateway compatibility and permissions  |
| `citadel-agent bootstrap`          | Create the agent's own access contract                            |
| `citadel-agent llm-check`          | Verify the agent's model access through the gateway               |
| `citadel-agent chat`               | Chat with the agent in the terminal                               |
| `citadel-agent devui`              | Use the agent in the Agent Framework DevUI (local development)    |
| `citadel-agent usage`              | Summarize model usage per request from the local usage log        |
| `citadel-agent contracts validate` | Validate a contracts repository (used by its pull request checks) |
| `citadel-agent contracts render`   | Regenerate the generated files of a contract from its intent      |
| `citadel-agent contracts whatif`   | Preview a change set's deployments                                |
| `citadel-agent contracts deploy`   | Deploy a merged change set (used by the deploy workflow)          |

## Documentation

| Guide                                             | For                                                        |
|---------------------------------------------------|------------------------------------------------------------|
| [Quickstart](docs/quickstart.md)                  | Application teams using the gateway                        |
| [Operator guide](docs/operator-guide.md)          | Platform teams: admin policy, modes and operator workflows |
| [Pull request deployments](docs/gitops-setup.md)  | Contracts repository CI/CD with GitHub Actions and OIDC    |
| [Publishing tools and agents](docs/publishing.md) | MCP and A2A publish contracts (preview)                    |
| [Security model](docs/security.md)                | Trust boundaries, approvals and secret handling            |
| [Token usage](docs/token-usage.md)                | Tool routing, compaction and measuring input tokens        |
| [Evaluations](docs/evaluations.md)                | Live behavior checks with a stubbed toolset                |
| [Validation notebooks](docs/validation.md)        | Running upstream's baseline notebooks and what they found  |
| [Upstream sync](docs/upstream-sync.md)            | Moving the agent to a newer upstream Citadel commit        |
| [Troubleshooting](docs/troubleshooting.md)        | Network, permissions, proxies and what-if noise            |
| [Costs and teardown](docs/costs-and-teardown.md)  | Hub cost, model pricing and removing everything            |

## Upstream pin

The agent is built and tested against a specific upstream commit. The commit and the upstream `release.json` version tracks are recorded in `citadel.lock.json`.

| Command                         | Purpose                                                       |
|---------------------------------|---------------------------------------------------------------|
| `citadel-agent version`         | Show the agent version and the pinned upstream release tracks |
| `citadel-agent upstream status` | Confirm the submodule checkout matches `citadel.lock.json`    |
| `citadel-agent upstream lock`   | Record the checked-out submodule commit in the lock file      |
| `citadel-agent upstream sync`   | Compare with newer upstream; `--apply` moves the pin if safe  |

`upstream sync` is a dry run by default: it diffs release tracks, template parameters and docs, runs the drift checks against the new commit and rates it SAFE, REVIEW or BLOCKED (`--report UPSTREAM-COMPAT.md` writes the details; `--check` exits 1 when BLOCKED). The weekly `upstream-sync` workflow runs it with `--apply` and opens a pull request.

## Project layout

| Path                      | Contents                                                      |
|---------------------------|---------------------------------------------------------------|
| `src/citadel_agent/`      | Agent source code                                             |
| `upstream/ai-hub-gateway` | Pinned upstream Citadel repository (git submodule, read-only) |
| `citadel.lock.json`       | Pinned upstream commit and release tracks                     |
| `config/`                 | Example admin policy                                          |
| `tests/`                  | Unit tests                                                    |
| `skills/`                 | Agent Skills playbooks                                        |
| `templates/`              | Template for the canonical contracts repository               |
| `docs/`                   | User, admin and operations documentation                      |

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The live agent evaluations are opt in. They call a real model through the gateway with every tool stubbed:

```bash
$env:CITADEL_AGENT_EVALS = "1"
uv run pytest tests/evals -m eval
```
