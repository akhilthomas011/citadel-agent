---
title: Upstream sync
description: Move Citadel Agent to a newer upstream Citadel commit safely, with a compatibility report and a reviewable pull request
---

## Why the agent pins upstream

The agent never copies upstream infrastructure code, policies or documentation. It reads them from a pinned git submodule at `upstream/ai-hub-gateway`, and `citadel.lock.json` records the pinned commit, its `release.json` version tracks and the sync date. Moving the pin is the one way to adopt new upstream behavior, and it should be a reviewed change.

## Check the current pin

```bash
uv run citadel-agent version
uv run citadel-agent upstream status
```

`status` confirms that the submodule checkout matches the lock file.

## Run a sync

```bash
uv run citadel-agent upstream sync --report UPSTREAM-COMPAT.md
```

The default is a dry run. It fetches the target ref (by default the tip of the locked branch), exports the old and new trees into a scratch directory and compares them. The submodule checkout and the lock file stay untouched.

The report covers:

* Old and new commits
* Each release track with old value, new value and compatibility level
* Parameter surface changes for the access, backend, publish and alert templates
* Documentation section changes
* Entries in the agent's catalogs that refer to something that disappeared upstream
* Results of the drift checks against the new commit

| Verdict | Meaning                                                                                        |
|---------|------------------------------------------------------------------------------------------------|
| SAFE    | No incompatible track, no drift, no parameter changes and no catalog findings                  |
| REVIEW  | A warning, a parameter change, removed documentation or checks that were skipped               |
| BLOCKED | An incompatible release track, a drift error or a catalog entry that no longer exists upstream |

Preview tracks (publish contract and gateway upgrade) need an exact match. A changed preview track blocks writes, so the sync is BLOCKED until the agent has been tested against it.

## Options and exit codes

| Option                  | Effect                                                         |
|-------------------------|----------------------------------------------------------------|
| `--ref <sha or branch>` | Compare with a specific ref instead of the locked branch tip   |
| `--report <path>`       | Write the Markdown report                                      |
| `--check`               | Exit 1 when the verdict is BLOCKED, for use in CI              |
| `--apply`               | Move the submodule to the new commit and rewrite the lock file |

`--apply` refuses to move the pin when the verdict is BLOCKED, and it still writes the report. If the checkout fails, it leaves the lock file alone. Exit code 2 means a configuration problem, such as a missing lock file, an uninitialized submodule or an unknown ref.

> [!NOTE]
> If the Bicep CLI is missing or the new templates do not compile, the parameter comparison is skipped and the verdict is REVIEW, not BLOCKED. Read the report before applying.

## The weekly workflow

`.github/workflows/upstream-sync.yml` runs every week and on demand:

1. The read-only `sync` job installs Bicep and the dependencies, runs `upstream sync --apply --report UPSTREAM-COMPAT.md` and runs the test suite.
2. The `propose` job, which has write permission, pushes a branch `upstream-sync/<short-sha>` and opens a pull request, or updates the existing one.
3. The pull request is a draft when the verdict is REVIEW or BLOCKED, or when tests fail, and its title says which.

For a BLOCKED result the pin does not move, and the pull request carries only the report.

Enable "Allow GitHub Actions to create and approve pull requests" in the repository settings, or the workflow cannot open the pull request.

## After merging a new pin

1. Update the contracts repository's submodule in a dedicated pull request, after the agent version you use has passed its tests against the new commit.
2. Regenerate contracts with `citadel-agent contracts render` and review the diff. Generated files record the upstream commit they came from.
3. Update `CITADEL_AGENT_PACKAGE` in the contracts repository to the new agent commit. See [pull request deployments](gitops-setup.md).
