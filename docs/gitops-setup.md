---
title: Set up pull request deployments
description: Configure the contracts repository so pull requests are validated, previewed and deployed to Azure with GitHub Actions and OpenID Connect
---

## Overview

In pull request mode the agent never deploys. It opens a pull request in the contracts repository, and GitHub Actions validates, previews and deploys the change. Production always works this way. Development and test can use it too.

Three workflows ship in `templates/contracts-repo/.github/workflows/`:

| Workflow           | Trigger                                  | Azure credentials | Purpose                                                      |
|--------------------|------------------------------------------|-------------------|--------------------------------------------------------------|
| `citadel-validate` | Every pull request                       | None              | Schema, generated files, policy lint, caps, secrets, compile |
| `citadel-whatif`   | A maintainer adds the `whatif` label     | What-if identity  | Preview the change with the trusted base-branch workflow     |
| `citadel-deploy`   | A merge that adds a change set to `main` | Per environment   | Deploy each step of the change set in order                  |

## Publish the agent package

The workflows check out an agent commit and install its locked production and
build dependencies from wheels using `uv sync --locked --no-dev --group build
--no-install-project --no-build --python 3.11`. A second sync installs the agent
with `--no-editable --no-build-isolation`, using the locked build environment.
Publish the agent source with its reviewed public-index `uv.lock` to a repository
the runner can read and pin an exact 40-character commit:

1. Push the agent repository to GitHub. A public repository needs no credentials on the runner.
2. Set a repository variable in the contracts repository:

   ```bash
   gh variable set CITADEL_AGENT_PACKAGE --repo <org>/<contracts-repo> \
     --body "git+https://github.com/<org>/<agent-repo>@<commit-sha>"
   ```

3. Move the pin only after the new agent version passes its tests. Template hashes depend on the template sources, not on the compiler, so a newer pin can deploy changes prepared by an older agent.

Only `git+https://github.com/<owner>/<repository>@<40-character-commit>` references
are accepted, not branch names, package version ranges or arbitrary URLs. The
installer checks the fetched SHA, initializes pinned submodules and installs
before Azure sign-in. It ignores runner-local uv index configuration. Update
all three workflow copies in existing contracts repositories when adopting this
installation model; a template change does not propagate automatically.

## Create the Azure identities

Create one Microsoft Entra application per GitHub environment, with a federated credential for the repository.

```bash
az ad app create --display-name citadel-contracts-whatif --sign-in-audience AzureADMyOrg
az ad sp create --id <app-id>
```

The federated credential subject must match what GitHub presents. GitHub can include immutable owner and repository IDs, for example:

```text
repo:<owner>@<owner-id>/<repo>@<repo-id>:environment:citadel-whatif
```

If sign-in fails with `AADSTS700213`, copy the subject from the error message into the credential. A new credential can take a few minutes to propagate, so retry a failed first sign-in before changing anything.

### Roles

Keep every resource role on the hub's resource group, not the subscription. Subscription scope is only for a custom role that allows deployment records:

```json
{
  "Name": "Citadel Contracts Deployer",
  "Actions": ["Microsoft.Resources/deployments/*"],
  "NotActions": [],
  "AssignableScopes": ["/subscriptions/<subscription-id>"]
}
```

Assign these roles to the deployment identity:

* Citadel Contracts Deployer on the subscription
* Reader, API Management Service Contributor and Monitoring Contributor on the hub resource group
* Key Vault Contributor and Key Vault Secrets Officer on the hub Key Vault

> [!IMPORTANT]
> Azure what-if checks the same write permissions as a deployment, including the nested module deployments. The what-if identity therefore needs the same roles as the deployment identity. It never deploys, so protect it with the `whatif` label and with environment reviewers instead of with a smaller role set.

## Configure GitHub

Set repository variables:

| Variable                 | Value                                             |
|--------------------------|---------------------------------------------------|
| `CITADEL_AGENT_PACKAGE`  | Pinned `git+https://...@<commit>` agent reference |
| `AZURE_TENANT_ID`        | Entra tenant ID                                   |
| `AZURE_SUBSCRIPTION_ID`  | Subscription that holds the hub                   |
| `AZURE_WHATIF_CLIENT_ID` | Application ID of the what-if identity            |

Create the GitHub environments `citadel-whatif` and `citadel-<env>` for every environment in the admin policy, and set `AZURE_CLIENT_ID` in each `citadel-<env>` environment to that environment's deployment identity.

Add the `whatif` label once:

```bash
gh label create whatif --color 0E8A16 --description "Run the privileged Citadel what-if preview"
```

> [!WARNING]
> Required reviewers on environments need a GitHub plan that supports them for the repository. Private repositories on some plans reject the setting. Without reviewers, only people who can add labels and merge can trigger privileged workflows, so restrict write access to the contracts repository and enable branch protection with CODEOWNERS.

## How a change flows

1. The agent prepares the change, runs what-if locally and opens a pull request that carries a change-set manifest in `.citadel/changesets/`.
2. `citadel-validate` regenerates every file from the intents and fails if anything differs. Agent pull requests may only change `contracts/` and `.citadel/{changesets,rotations,locks}/`.
3. A maintainer adds the `whatif` label. The workflow runs from the base branch, takes only contract data from the pull request, and writes the preview to the job summary.
4. A reviewer merges. `citadel-deploy` finds the new manifests, skips any that were deployed directly, and deploys each environment in order.

Before deploying a step, the pipeline compiles the parameters again and compares hashes with the manifest. A mismatch stops the deployment and the log names which hash differs.

## Troubleshooting

| Symptom                                                        | Cause and fix                                                                   |
|----------------------------------------------------------------|---------------------------------------------------------------------------------|
| Validate fails: `Set the repository variable`                  | `CITADEL_AGENT_PACKAGE` is empty. Set it to a pinned commit.                    |
| What-if cannot fetch the pull request head                     | The workflow needs its read-only token for the fetch. Use the shipped template. |
| `AADSTS700213` on Azure login                                  | The federated credential subject differs from GitHub's. Copy it from the error. |
| What-if lists `Authorization failed for template resource`     | The what-if identity lacks the write roles. Grant the deployment roles.         |
| Deploy: `compiled output differs from the prepared change set` | The message names `parameters` or `template`. Re-prepare the change.            |
| Validate: `differs from the prepared change set`               | A contract file was edited by hand. Regenerate with `contracts render`.         |
