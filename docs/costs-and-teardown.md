---
title: Costs and teardown
description: Understand what a Citadel development hub costs, seed model pricing for cost reports and remove everything when you are done
---

## What it costs

A Citadel hub is not a small deployment. The development hub this agent was built against uses API Management Standard v2, which is roughly USD 910 per month before the supporting services: Cosmos DB, Event Hubs, a Logic App, Application Insights, Log Analytics, Key Vault and the Foundry accounts with their model deployments. Token charges for the models come on top.

Plan for the cost before you deploy, and tear the hub down when nobody is using it.

## Cost reports need model pricing

Usage reports read token counts from the Cosmos DB `llm-usage-container`. Cost estimates also need the `model-pricing` container, which is empty after a deployment. Without prices, the report shows usage and lists the models it could not price.

Upstream treats pricing as an operator-reviewed step. Its sample file, `src/usage-reports/model-pricing-generated-extended.json`, lists public prices at the time it was written. The `pricing seed` command previews and writes the entries for the models your gateway serves:

```bash
uv run citadel-agent pricing seed
uv run citadel-agent pricing seed --apply
```

The first command is a preview and changes nothing. It reads the gateway's live backend contract, keeps the sample entries for models it serves and compares them with the container. It reports:

* New prices, and prices that would change, with the old and new values side by side
* Models in the file that the gateway does not serve, which are left out
* Entries priced at zero, which are not written because a zero would show as a cost of 0 instead of "unknown"
* Models the gateway serves that still have no price, whose usage shows without a cost

Aliases such as `chat-default` need no price, because usage is recorded under the real model. Running the command again writes nothing when the container is up to date, and an existing entry for a model is updated in place instead of duplicated. Pass `--file` to use your own reviewed file, and `--all` to seed models the gateway does not serve.

Before you apply:

1. Review every price against the published price for the model. The sample is a starting point, and some models in it carry placeholders or prices that were not checked. On the development hub, only the embedding price could be confirmed against the Azure retail price list, so treat the others as unverified.
2. Make sure the deployment names match what the gateway reports, because usage records join on them.
3. Writing needs a Cosmos DB data role such as Cosmos DB Built-in Data Contributor, and a network path to the account. Scope the role to the `model-pricing` container, not the whole account.

> [!IMPORTANT]
> Prices drive chargeback numbers, so a person should review them before `--apply`. Costs in reports are estimates from that table, not invoices. Seeding is an operator task, and the command refuses for the consumer persona.

To check the result, ask the agent for a usage report. A 30-day report on the development hub showed 637,283 prompt tokens, of which 330,240 were cached, and 15,233 completion tokens at an estimated cost of 0.5791 USD, which matches the arithmetic from the stored prices.

## Remove the development hub

From the directory that holds the upstream clone and the `azd` environment:

```bash
azd down --purge
```

`--purge` also removes soft-deleted Key Vault and Cognitive Services resources, which would otherwise keep their names reserved.

## Remove the pipeline identities

If you created the GitHub OpenID Connect identities, remove them as well:

```bash
az ad app delete --id <whatif-app-id>
az ad app delete --id <deploy-app-id>
az role definition delete --name "Citadel Contracts What-If"
az role definition delete --name "Citadel Contracts Deployer"
```

Deleting an application removes its federated credentials. Then delete the GitHub variables `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_WHATIF_CLIENT_ID` and each environment's `AZURE_CLIENT_ID`.

## Remove development network exceptions

Development setups often allow a developer's public address on Key Vault and Cosmos DB. Remove those rules when you finish:

```bash
az keyvault network-rule remove --name <vault-name> --ip-address <address>
az cosmosdb update --name <account-name> --resource-group <resource-group> --ip-range-filter "<remaining-addresses>"
```

## Local state

`.citadel-agent/` holds sessions, prepared changes, the audit log and the usage log. It is ignored by git and safe to delete when you want a clean start. Delete `sessions/` first if you pasted something sensitive into a conversation.
