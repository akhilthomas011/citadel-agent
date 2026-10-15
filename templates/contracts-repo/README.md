---
title: Citadel contracts repository
description: Canonical, version-controlled source of truth for Citadel Governance Hub contracts managed by Citadel Agent
---

## Overview

This repository holds every Citadel Governance Hub contract for your organization: backend contracts (models), access contracts (use cases), publish contracts (MCP tools and A2A agents) and alerts. Citadel Agent proposes changes here as pull requests, or records changes it deployed directly to development environments. Deployments always use the upstream Citadel templates from the pinned `upstream/` submodule.

## Layout

| Path                                     | Contents                                                     |
|------------------------------------------|--------------------------------------------------------------|
| `upstream/ai-hub-gateway`                | Pinned upstream Citadel repository (git submodule)           |
| `contracts/access/<bu>/<use-case>/<env>` | Access contract intent plus generated parameters and policy  |
| `contracts/backend/<env>`                | The single authoritative backend contract of an environment  |
| `contracts/publish/<name>/<env>`         | Publish contract intent plus generated parameters            |
| `contracts/alerts/<scope>/<env>`         | Alert rules intent plus generated parameters                 |
| `.citadel/admin-policy.yaml`             | Admin policy read by Citadel Agent (protected by CODEOWNERS) |
| `.citadel/changesets`                    | Change-set manifests the deployment workflow executes        |
| `.citadel/rotations`, `.citadel/locks`   | Key-rotation transactions and change locks                   |

Each contract folder contains `contract.yaml` (the intent, edited by people or the agent) and generated files (`main.bicepparam`, `*-product-policy.xml`) that carry a "generated" header. Never edit generated files by hand: change the intent and regenerate.

## Set up

1. Create a new repository from this template.
2. Add the upstream Citadel repository as a pinned submodule:

   ```bash
   git submodule add -b citadel-v1 https://github.com/Azure-Samples/ai-hub-gateway-solution-accelerator.git upstream/ai-hub-gateway
   git -C upstream/ai-hub-gateway checkout <commit-tested-by-your-agent-version>
   ```

3. Complete `.citadel/admin-policy.yaml` with your environments and commit it.
4. Replace the team names in `.github/CODEOWNERS` and protect the `main` branch: require pull requests, CODEOWNERS review and the `citadel-validate` check.
5. Create GitHub environments `citadel-whatif` and `citadel-<env>` for each environment, with required reviewers.
6. Create one Microsoft Entra workload identity per GitHub environment with federated credentials for this repository. GitHub can issue subjects that include immutable owner and repository IDs, so copy the subject from the first failed sign-in error (`AADSTS700213`) if the credential does not match. Grant least privilege on the hub's resource group, not the subscription:
   * Deployment identities need a custom role limited to `Microsoft.Resources/deployments/*` (subscription scope, no resource access), API Management Service Contributor and Monitoring Contributor on the resource group, and Key Vault Contributor plus Key Vault Secrets Officer on the target Key Vault (Contributor on Foundry resource groups when Foundry connections are used)
   * The what-if identity needs the same roles, because ARM what-if checks the same write permissions as a deployment. It never deploys, so protect it with the `whatif` label and required environment reviewers
   * Entra can take a few minutes to propagate a new federated credential; retry a failed first sign-in
7. Set repository variables `CITADEL_AGENT_PACKAGE` (a `git+https://github.com/<owner>/<repo>@<40-character-commit>` reference to an agent commit containing its reviewed public-index `uv.lock`), `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` and `AZURE_WHATIF_CLIENT_ID`, and the environment variable `AZURE_CLIENT_ID` in each `citadel-<env>` environment. Jobs check out that agent SHA and use `uv sync --locked --no-dev --no-editable` before Azure sign-in; a floating package or branch reference is rejected.

## Workflows

| Workflow           | Trigger                            | Credentials     | Purpose                                                      |
|--------------------|------------------------------------|-----------------|--------------------------------------------------------------|
| `citadel-validate` | Every pull request                 | None            | Schema, generated files, policy lint, caps, secrets, compile |
| `citadel-whatif`   | Maintainer adds the `whatif` label | What-if         | Preview changes with the trusted base-branch workflow        |
| `citadel-deploy`   | Merge that adds a change set       | Per environment | Deploy each change-set step in order                         |

> [!IMPORTANT]
> Agent pull requests (branches starting with `citadel-agent/`) may only change `contracts/` and `.citadel/{changesets,rotations,locks}/`. Changes to the admin policy, workflows or the upstream pin need a separate pull request reviewed by platform admins.

## Upgrading upstream Citadel

Bump the `upstream/ai-hub-gateway` submodule in a dedicated pull request after the Citadel Agent version you use has been tested against the new commit. Regenerate all contracts with the agent and review the resulting diff before merging.
