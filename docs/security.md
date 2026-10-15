---
title: Security model
description: What Citadel Agent protects, where its guardrails stop and which controls enforce the real boundaries
---

## Trust model

Citadel Agent runs on the user's machine with the user's own Azure credentials. The admin policy and the persona logic are guardrails that keep an honest user and a well-behaved model inside the lines. They are not a security boundary against someone who edits the code.

The boundaries that hold are outside the agent:

* Azure RBAC and data-plane roles on API Management, Key Vault, Cosmos DB and the resource group
* Network rules on Key Vault, Cosmos DB and Foundry
* Branch protection and CODEOWNERS on the contracts repository
* GitHub environment approvals and federated identities scoped to one repository and environment
* Production deployments that only the pipeline performs

A hosted version would enforce the admin policy on the server. Until then, treat local mode as a safety net, not as access control.

## Changes need approval

Every change follows the same path, and each step narrows what can go wrong:

1. The change is validated against the admin policy caps and the contract schema.
2. Preparation renders the files, compiles them, scans the compiled parameters for secrets and runs what-if. The result is stored as an immutable change set with content hashes.
3. Approval shows the stored change set: the summary, the what-if, and a diff of the contract values that change (owners, allowed models, limits, protections), read from the stored snapshot and compared with the repository base commit. The what-if lists resources and property paths but never values, so the diff is what shows that a change adds an owner or widens the allowed models. Text the model wrote, such as the summary, is reduced to one printable line, so it cannot forge extra lines or hide text with control characters.
4. Execution re-checks the approval hash, the repository base commit, the admin policy blob, the upstream pins, the live gateway version and the what-if fingerprint. Any drift invalidates the approval.
5. A change set runs once. A second attempt is refused.

An approval request whose hash does not match the stored change is refused automatically, without asking the user.

### Approval in the DevUI

The DevUI approval card shows only the raw call, and it cannot ask for typed confirmation. Two safeguards close those gaps:

* Before every `execute_change` approval card, the DevUI conversation shows the stored change set (summary, what-if, the contract diff, approval hash and what approval will do), read from disk and not from the model. A request that does not match a stored change is flagged with a warning to reject it.
* An irreversible step, regenerating the old key in a rotation, is refused if it is approved in the DevUI, because the typed `ROTATE` confirmation is not possible there. Approve that step in `citadel-agent chat`.

The terminal chat renders the same stored change in its own approval prompt.

## Secrets

Secrets are blocked structurally, then redacted as a second layer.

* Contract intents reject literal secret values. Only Key Vault references are accepted.
* The compiled parameters of every change are scanned for secret-shaped values before approval.
* Tools return Key Vault secret names and code that reads the secret, never the value.
* Output from every tool passes through a redactor that removes keys, bearer tokens, connection strings and SAS tokens before the model sees it.
* The agent instructs the model never to repeat a pasted secret and to advise rotation. The evaluation suite checks this.

> [!NOTE]
> The agent cannot prevent you from typing a secret into the chat. A pasted secret reaches the model through the gateway and is stored in the local session file. Rotate it.

## Local files

| Location                     | Contents                                               | Sensitivity                        |
|------------------------------|--------------------------------------------------------|------------------------------------|
| `.citadel-agent/sessions/`   | Conversations, including your messages and tool output | Treat as sensitive. Ignored by git |
| `.citadel-agent/changes/`    | Prepared change sets and snapshots                     | Contains contract data, no secrets |
| `.citadel-agent/audit/`      | JSON Lines audit log of approvals and executions       | No secret values                   |
| `.citadel-agent/usage.jsonl` | Token counts per model request                         | Counts and tool names only         |
| `.env`                       | Local settings                                         | May hold a development gateway key |

## Pipeline controls

The shipped workflows follow a conservative model:

* Validation runs without credentials.
* The privileged what-if workflow runs the base branch's definition (`pull_request_target`), takes only contract data from the pull request and needs a maintainer to add a label.
* A change-set manifest is data that a pull request controls, and the what-if and deploy jobs run with credentials. A manifest step may only deploy a generated `contracts/<kind>/.../main.bicepparam` that uses an upstream template, and for a manifest that can still run, the step must equal what the renderer produces for that contract (template, subscription and location). Validation enforces this, and `contracts whatif` and `contracts deploy` enforce it again before they compile or deploy anything, because the deploy workflow does not run validation first.
* Nothing outside the pinned upstream may add a template, a Bicep configuration file or a parameter file, and the change-set folder may only hold JSON manifests. A template or parameter file could otherwise be compiled with credentials in the environment.
* `extraParameters` in a contract can add upstream parameters the schema does not model, but it cannot override a parameter the renderer computes, such as limits, models, service codes or the environment's coordinates. Caps are checked on the typed intent, so an override would otherwise bypass them.
* The deploy workflow reads change-set file names as NUL-separated data and accepts only the exact pattern the agent writes, and it passes paths to the shell through environment variables, never through the script text. A file name cannot inject a command.
* Agent pull requests may only change `contracts/` and `.citadel/{changesets,rotations,locks}/`. Changes to the admin policy, the workflows or the upstream pin need a separate review.
* Actions are pinned by commit SHA.
* Azure sign-in uses OpenID Connect with a federated credential limited to one repository and environment. No client secrets exist.
* The deployment compares hashes of the compiled parameters and of the template sources before it deploys.

Known limits to plan for:

* The what-if identity needs the same write roles as the deployment identity, because Azure what-if checks write permissions. Protect it with the label and with environment reviewers.
* GitHub environment reviewers need a plan that supports them. Without them, restrict write access to the contracts repository.
* The contracts workflows accept only a GitHub HTTPS agent reference pinned to a full commit SHA. Before Azure sign-in, they check out that commit and install runtime/build dependencies from wheels using its committed `uv.lock` and build group. They then install the agent non-editably with build isolation disabled, using those locked build tools. A package pin or exact build-backend version alone does not lock transitive dependencies. Missing wheels fail closed rather than building an unreviewed dependency.
* Existing contracts repositories must adopt the updated workflows and a lock-bearing agent commit. Merely updating this repository does not secure an older deployed workflow.

### Production readiness

Production requires enforced pull requests, required validation and CODEOWNERS
reviews, restricted bypass privileges, deployment-environment reviewers and
environment-scoped OIDC. A `prod` policy setting alone does not establish those
controls. If the repository plan cannot enforce them, treat that configuration
as trusted-operator development only.

Changing a private contracts repository to public is a separate disclosure
decision. Review reachable history, contract ownership/contact information,
endpoints and infrastructure identifiers before changing visibility. Removing
data from the latest commit does not remove it from history. Publishing does
not itself enable branch protection or configure Azure identities.

## Agent's own model access

The agent calls its model through the Citadel gateway with its own access contract, so its usage is governed, limited and attributed like any other. It sends the `api-key` header and an `x-app-id` of `citadel-agent`. Its contract limits both the models it may call and its token budget. Prompts and responses are not exported by telemetry.

## Reporting a problem

If you find a way to make the agent bypass an approval, reveal a secret or exceed the admin policy, report it to the repository owner and include the prepared change set ID. Do not open a public issue with secret material.
