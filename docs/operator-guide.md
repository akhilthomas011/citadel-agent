---
title: Operator guide
description: Set up Citadel Agent for a platform team, including the admin policy, the contracts repository, direct and pull request modes, and operator workflows
---

## Roles

Citadel Agent derives a persona from Azure permissions instead of letting people choose one.

| Persona  | How it is derived                                                                                | Capabilities                                                                            |
|----------|--------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------|
| Consumer | Any signed-in user                                                                               | Own use cases: discover, code, access contracts, diagnose, usage, keys, use-case alerts |
| Operator | Effective write permission on the API Management instance, or membership of a listed Entra group | Everything, including backends, publishing and gateway-wide alerts                      |

The persona decides which tools the model sees. Every tool and the change engine check authorization again, so hiding a tool is only the first layer.

> [!IMPORTANT]
> In local mode the admin policy and personas are guardrails, because the user runs the code with their own credentials. The real boundaries are Azure RBAC, data-plane roles and network rules, branch protection with CODEOWNERS, and pipeline environment approvals. See the [security model](security.md).

## Set up an environment

The agent works with an existing Citadel hub and a canonical contracts repository.

1. Describe the hub. The command reads the resources in the hub's resource group and prints an `environments` entry:

   ```bash
   uv run citadel-agent env discover --subscription <subscription-id> --resource-group <hub-resource-group>
   ```

2. Copy `config/admin-policy.example.yaml`, paste the discovered environment and adjust limits and modes.
3. Create the contracts repository, pinned to the same upstream commit as the agent:

   ```bash
   uv run citadel-agent repo init --path ../citadel-contracts --policy admin-policy.yaml
   ```

4. Push it to your git host. Protect `main`, require review and add CODEOWNERS for `.citadel/`, the workflows and `upstream/`.
5. Point the agent at it in a `.env` file, which is ignored by git:

   ```text
   CITADEL_AGENT_REPO_URL=https://github.com/<org>/citadel-contracts.git
   ```

6. Verify, then create the agent's own access contract so its model calls go through the gateway:

   ```bash
   uv run citadel-agent doctor
   uv run citadel-agent bootstrap
   uv run citadel-agent llm-check
   ```

## The admin policy

The policy lives at `.citadel/admin-policy.yaml` in the contracts repository. The agent reads the committed version on the protected base branch and ignores local edits. A missing, invalid or stale policy makes the agent read-only.

Each environment declares:

| Field                      | Meaning                                                                       |
|----------------------------|-------------------------------------------------------------------------------|
| `class`                    | `dev`, `test` or `prod`                                                       |
| `mode`                     | `direct` (deploy after approval, then record) or `pr`                         |
| `fallbackToPr`             | Open a pull request when the caller cannot deploy directly                    |
| `caps`                     | Maximum tokens per minute and monthly quota, allowed models and service codes |
| `allowPrerequisiteActions` | Reserved. Prerequisites stay manual today, and `prod` forbids the setting     |

Hard rules: a `prod` environment cannot use `direct` mode and cannot allow direct-output credentials. These are validation errors, not warnings.

## Direct and pull request modes

| Aspect        | `direct` (dev and test only)                             | `pr`                                           |
|---------------|----------------------------------------------------------|------------------------------------------------|
| Who deploys   | The agent, with the user's credentials, after approval   | The pipeline, with its own federated identity  |
| Record in Git | A pull request labelled `deployed-directly`              | The pull request that triggered the deployment |
| Verification  | Hashes, what-if fingerprint and upstream pins re-checked | The same checks run again in the pipeline      |

In both modes the contracts repository is the source of truth. For direct mode you still merge the recording pull request so Git matches Azure. For a local repository, a direct change fast-forwards `main` and Git refuses if the repository has uncommitted changes.

Set up the pipeline with the [pull request deployment guide](gitops-setup.md).

## What the agent never does

* It never edits your working copy. It works in its own clone under `.citadel-agent/repo`.
* It never rewrites upstream templates. It deploys the pinned upstream Bicep through generated `.bicepparam` files.
* It never runs a change twice. Each change set executes once.
* It never grants Azure roles by itself. Missing role assignments become manual prerequisite steps with exact commands.

## Operator workflows

Ask the agent in plain words. These workflows are available to operators:

| Workflow                     | What happens                                                                                            |
|------------------------------|---------------------------------------------------------------------------------------------------------|
| Add or change a backend      | Discover deployments, check the gateway identity's roles, merge into the authoritative backend contract |
| Model aliases                | Add or remove an alias such as `chat-default` that points to several models                             |
| Circuit breaker and affinity | Change priorities, weights, circuit-breaker and session-affinity settings                               |
| Gateway-wide alerts          | Create alert rules with no product filter                                                               |
| Use-case alerts              | Create alerts for one product, plus the contract changes that emit the events                           |
| Version and drift            | Compare the live gateway with the agent's tested versions                                               |

The first backend change imports the live backend contract, but only if it contains no inline secrets. After that, the repository's backend intent is authoritative, and every update merges into it.

> [!NOTE]
> Removing a model that access contracts still use produces an impact warning before anything is prepared.

## Costs

Usage reports need Cosmos DB access. Cost estimates also need the `model-pricing` container. See [costs and teardown](costs-and-teardown.md).
