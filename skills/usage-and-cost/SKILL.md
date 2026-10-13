---
name: usage-and-cost
description: Report token usage and estimated cost per use case, model and application from the gateway's usage data.
metadata:
  persona: any
  docs: "guides/platform-observability-guide.md#choosing-the-right-signal guides/platform-observability-guide.md#4-cosmos-db-usage-data--power-bi-finops--chargeback"
---

# Usage and Cost

## Overview

Use this skill for questions about token consumption, cost, throttling trends and remaining quota.

## Workflow

1. Clarify the scope: which use case (or all of the caller's use cases), environment, time range and grouping (model, application or day).
2. Call `usage_report`. It reads the Cosmos DB usage records and applies the model pricing table. When Cosmos DB is unreachable it falls back to Application Insights token metrics, which give usage but no cost.
3. Present a short table and the totals. Always state the data source, its freshness and whether the cost is an estimate.
4. Relate the numbers to the contract: compare peak usage with the tokens-per-minute limit and month-to-date usage with the token quota from `describe_access_contract`.
5. Suggest actions when useful: a higher limit, a cheaper model for simple tasks, or `x-app-id` headers to attribute usage per application.

## Decision rules

* Consumers see only their own products; operators can report across the gateway.
* Costs come from the gateway's pricing table and are estimates, not invoices.
* Usage data is ingested asynchronously and can lag by several minutes.

## Guardrails

* Do not expose other teams' usage to consumers.
* Do not present estimated cost as billing data.
