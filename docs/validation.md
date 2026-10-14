---
title: Validating a hub with upstream's notebooks
description: Run the four baseline validation notebooks from Citadel upstream against a hub, what to change so they run, and what they found on the development hub
---

## What the baseline is

Upstream ships validation notebooks and recommends running four of them, in order, on every new hub:

1. `llm-backend-onboarding-runner`: deploys the backend contract and tests every model
2. `citadel-universal-llm-api-all-models-tests`: calls every model through the Universal LLM API
3. `citadel-access-contracts-tests`: deploys three access contracts and load tests them
4. `citadel-agent-frameworks-tests`: runs Microsoft Agent Framework, Foundry Agent SDK and LangChain agents against those contracts

The notebooks deploy resources and, for the first one, replace the hub's whole backend contract. Run them against a development hub, never against one you cannot rebuild.

## Run them

`scripts/run_validation_notebooks.py` runs the notebooks headless and summarizes the results. Notebooks print most failures instead of raising them, so the summary counts exceptions and lines marked as failed.

1. Make a scratch copy of the pinned upstream tree that also holds the hub's azd environment. The notebooks write generated files next to the templates, and the copy keeps them out of the submodule:

   ```powershell
   robocopy <hub-clone> $env:TEMP\citadel-nb /E /XD .git .vscode .azdo assets /XF *.log
   ```

   The hub clone is the folder where you ran `azd up`. It holds `.azure/<environment>`.

2. Create an environment with the notebooks' requirements and the runner's tools:

   ```powershell
   uv venv $env:TEMP\nb-venv --python 3.11
   uv pip install --python $env:TEMP\nb-venv\Scripts\python.exe `
     -r $env:TEMP\citadel-nb\validation\requirements.txt nbclient nbformat ipykernel azure-keyvault-secrets
   ```

3. Take a snapshot of the live backend contract before you start, so you can see what the notebooks change.

4. Write a substitutions file for the values that do not fit your hub (see the next section), then run:

   ```powershell
   $env:AZD_SKIP_UPDATE_CHECK = "1"
   $env:PYTHONUTF8 = "1"
   & $env:TEMP\nb-venv\Scripts\python.exe scripts\run_validation_notebooks.py `
     --workdir $env:TEMP\citadel-nb --results $env:TEMP\nb-results `
     --subs-file subs.json --notebook citadel-access-contracts-tests
   ```

   Without `--notebook` the four baseline notebooks run in order. The results folder gets the executed notebooks, a transcript per notebook and `report.md`. Transcripts can contain data from your hub, so keep them out of Git.

5. Compare the backend contract with your snapshot and restore it if it changed.

> [!WARNING]
> Set `AZD_SKIP_UPDATE_CHECK=1`. When azd has an update available, one run blocked in the first cell until the cell timeout. The same call returned in about a second with the check disabled.

## Adapt the notebooks to your hub

The substitutions file is a JSON list of `{"pattern": ..., "replacement": ...}` applied to code cells. The runner reports how many replacements each one made, so a pattern that matched nothing is visible. These were needed on the development hub:

| Problem in the notebook                                                                                | Substitution                                                      |
|--------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------|
| Models in the contracts that the hub does not serve (`gpt-4.1`)                                        | Replace with a chat model the hub serves                          |
| Notebook 3 hardcodes the upstream author's Key Vault and Foundry (subscription, resource group, names) | Replace each value with `"REPLACE"` so azd fills in the hub's own |
| Notebook 1 defines its own model aliases that point at models the hub lacks                            | Replace the `model_aliases` list with the hub's live aliases      |

Every substitution is a deviation from upstream's notebooks. List yours with the results.

## Results on the development hub

| Notebook             | Result                                                                                                                                                               |
|----------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 1 Backend onboarding | Deployment succeeded. Chat models answered. One `408` timeout on `gpt-5.4-mini`.                                                                                     |
| 2 All-models tests   | 10 of 11 operations passed. `Mistral-Large-3` chat hit a 60 second client timeout.                                                                                   |
| 3 Access contracts   | All three contracts deployed after the Key Vault fix. Model access rules held. The load tests saw no throttling and two single errors, which match backend timeouts. |
| 4 Agent frameworks   | Agent Framework 55.6% reliable, LangChain 83.3%. The Foundry agent could not be reached.                                                                             |

### Failures that are not hub problems

* The image models `FLUX.2-pro` and `MAI-Image-2.5-Flash` return `404` on chat endpoints, because they only generate images. Notebook 1 sends every model a chat request. Its SDK and streaming cells use the first model in the list, so they fail for the same reason. Embedding and image models that the gateway can route return `400 operation unsupported`, which the notebook counts as a pass.
* Notebook 3 sends one model to all three contracts. The contract that does not list it is refused with `403 unauthorized_model_access`, which shows the model access rule works.
* The Foundry agent in notebook 4 fails with `403 Public access is disabled`. The hub's Foundry data plane is private only, so run that cell from inside the hub network.

### Defects in the upstream notebooks

* Notebook 3 hardcodes another subscription's Key Vault and Foundry values, and explicit values win over azd. Deployments failed with `CrossTenantDeploymentNotPermitted` until they were replaced.
* `validation/requirements.txt` omits `azure-keyvault-secrets`, which notebook 4 imports.

### Findings about the hub

**Intermittent slow model responses.** On both Foundry accounts, about one chat call in six took 10 to 45 seconds. In the gateway logs, the gateway's own overhead for slow requests averaged 3 to 28 ms, so the time is spent in the model deployment. Timeouts returned as `408` at almost exactly 21 seconds. `gpt-5.2` was consistently fast, while `gpt-5.4-mini` and `Mistral-Large-3` had slow tails. The probes and notebooks added some load during that window. Check the deployment capacity and the backend timeout setting before putting latency-sensitive workloads on these models.

**Stateful clients need session affinity.** The Agent Framework notebook uses the Responses API, which keeps conversation state on one backend. On a model served by two backends with no session affinity, 4 attempts across 5 calls failed with `previous_response_not_found`, and each call recovered only by retrying until it reached the right backend. Flag such models as session aware in the backend contract, or use Chat Completions. Upstream's notebook uses a model that sits on one backend, so it does not hit this.

### What the notebooks change

Notebook 1 flagged `gpt-5.4-mini` as session aware, which turned session affinity on for the whole hub. This differed from the contracts repository, so the committed backend contract was deployed again afterwards. A snapshot comparison confirmed no differences from before the run. If you want affinity, make it a deliberate change through the agent instead of a side effect of a test.

Notebooks 2 to 4 create test contracts in Azure that are not in Git. Remove them afterwards:

```powershell
az rest --method delete --url "<apim-resource-id>/products/<product-name>" `
  --url-parameters deleteSubscriptions=true api-version=2024-05-01
```

Use `--url-parameters` on Windows. An `&` inside the URL is read by `cmd.exe` as a command separator and the request loses its `api-version`.

Deleting the Key Vault secrets the notebooks wrote needs a secrets write role. Their keys stop working once the subscriptions are gone, so leaving them is harmless.
