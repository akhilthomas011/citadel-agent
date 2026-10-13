---
title: Costs and teardown
description: Understand what a Citadel development hub costs, seed model pricing for cost reports and remove everything when you are done
---

## What it costs

A Citadel hub is not a small deployment. The development hub this agent was built against uses API Management Standard v2, which is roughly USD 910 per month before the supporting services: Cosmos DB, Event Hubs, a Logic App, Application Insights, Log Analytics, Key Vault and the Foundry accounts with their model deployments. Token charges for the models come on top.

Plan for the cost before you deploy, and tear the hub down when nobody is using it.

## Cost reports need model pricing

Usage reports read token counts from the Cosmos DB `llm-usage-container`. Cost estimates also need the `model-pricing` container, which is empty after a deployment. Without prices, the report shows usage and lists the models it could not price.

Upstream treats pricing as an operator-reviewed step. The sample file is `src/usage-reports/model-pricing-generated-extended.json` in the upstream repository, and the post-deployment guide describes importing it.

1. Copy the sample from the pinned upstream under `upstream/ai-hub-gateway/src/usage-reports/`.
2. Review every price against the published prices for the models you deploy, and remove entries for models you do not use. The sample is a starting point based on list prices.
3. Set the `deploymentName` of each entry to the deployment name the gateway reports, because the usage records join on it.
4. Insert the documents into the `model-pricing` container, for example from the Azure portal's Data Explorer. Writing needs a Cosmos DB data role such as Cosmos DB Built-in Data Contributor and a network path to the account.
5. Ask the agent for a cost report. Estimates appear when a price matches.

> [!IMPORTANT]
> The agent does not seed prices for you. A price table drives chargeback numbers, so a person should review it. Costs in reports are estimates from that table, not invoices.

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
