---
name: key-rotation
description: Rotate a use case's subscription key with zero downtime as a guarded, multi-step transaction.
metadata:
  persona: any
  docs: "bicep/infra/citadel-access-contracts/access-contract-key-rotation-guide.md#-the-3-step-zero-downtime-rotation bicep/infra/citadel-access-contracts/access-contract-key-rotation-guide.md#-verification bicep/infra/citadel-access-contracts/access-contract-key-rotation-guide.md#-notes--troubleshooting"
---

# Key Rotation

## Overview

Each Citadel subscription has a primary and a secondary key, and the contract publishes one of them to Key Vault. Rotation switches clients to the other key, regenerates the old one, and restores normal settings. Each step is a deployment of the same access contract.

## Workflow

1. Call `start_key_rotation` for the use case and environment. It locks the contract against unrelated changes and records the rotation state in the contracts repository.
2. Step 1, switch: the tool prepares the contract with the other key published. Get approval and execute. All clients must now read the new key from Key Vault.
3. Wait for the owners to confirm that every client uses the new key (restart or refresh the secret). Ask explicitly; do not assume.
4. Step 2, rotate: call `advance_key_rotation`. Regenerating the old key cannot be undone, so the tool asks the user to type the contract name to confirm.
5. Step 3, maintenance: `advance_key_rotation` turns regeneration off again. This step is required; otherwise every later deployment would regenerate the key.
6. Report completion. `advance_key_rotation` verifies each step from the deployment outputs before moving on.

## Decision rules

* Roll back by switching to the original key, which is only possible before step 2.
* In pull request mode each step is a separate pull request; the next step starts only after the previous deployment succeeded.
* Mirrored gateways in resiliency contracts receive the same key automatically.

## Guardrails

* Never show key values. Verification compares keys in memory only.
* Do not skip the owner confirmation or the maintenance step.
