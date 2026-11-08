---
title: FinOps analytical parity and deliberate differences
description: Traceability from the pinned Power BI report to the six portal views, including numerical semantics, privacy and acceptance limits.
ms.date: 2026-11-06
---

## Reference and verification boundary

The reference is the pinned
[Citadel Power BI report](../upstream/ai-hub-gateway/src/usage-reports/Citadel-Governance-Hub-Usage-Dashboard-V1.1-Incremental.pbix)
at upstream commit `23fbc8fe7f2f`. Its SHA-256 is
`e853b6f11af8e519b736601cb5f50f98b8978c5613f0e92ffd1d2ad0f94603e0`.

The report's PBIR definitions contain six pages and 44 data-bound visuals.
Page/visual projections and the actual DAX measure/calculated-column definitions
were inspected locally. No customer source data was uploaded or modified.
Controlled tests reproduce the decoded arithmetic and deliberate differences
below; this is not a live Power BI Desktop or customer-Cosmos acceptance result.

## Visual traceability

Every data-bound visual is covered below. Decorative text, shapes and page
navigation are not financial measures. The portal retains three top-level
product pages and presents these six reference pages as FinOps tabs.

| Reference page                   | Reference visuals / measures                       | Count | Portal equivalent / deliberate alternative                                 |
| -------------------------------- | -------------------------------------------------- | ----- | ------------------------------------------------------------------------- |
| Utilization                      | UTC date slicer                                    | 1     | Shared UTC half-open reporting window                                      |
| Utilization                      | `CostUSDX` card                                    | 1     | Priced estimated cost card, separated by currency                           |
| Utilization                      | Product/deployment bars and token-share pies        | 3     | Proportional bars and exact product/deployment utilization table            |
| Utilization                      | Product and deployment count cards                 | 2     | Explicitly named distinct observed labels, excluding absent labels          |
| Utilization                      | Source `totalTokens` card                          | 1     | Source total tokens, unavailable if any contributing total is missing       |
| Utilization                      | Prompt/cached/audio and completion/audio/reasoning  | 2     | Extended token composition, including prediction and image counts           |
| Heatmap                          | UTC date slicer                                    | 1     | Shared reporting window                                                    |
| Heatmap                          | Product and deployment treemaps                    | 2     | Accessible proportional bars, product/deployment heat tiles and exact table |
| Heatmap                          | Daily deployment tokens/cost combo chart           | 1     | Separate daily token/cost trends plus exact daily deployment table          |
| Usage and Cost Overview          | Date and model slicers                             | 2     | Shared window and attribution filters                                      |
| Usage and Cost Overview          | Daily total-token and cost lines                   | 2     | Hourly/daily UTC timelines with gaps for unavailable data                    |
| Usage and Cost Overview          | `GrandTotalTokens` and cost cards                  | 2     | Shared filtered totals/cost cards; product-filter difference below           |
| Usage and Cost Overview          | Date/token/cost table                              | 1     | Exact hourly/daily usage and cost table                                     |
| Usage and Cost Overview          | Hour-of-day token/cost columns                     | 1     | Hour-of-day measure timeline and exact token/cost table                     |
| Model and Deployment Performance | UTC date slicer                                    | 1     | Shared reporting window                                                    |
| Model and Deployment Performance | Token and cost cards                               | 2     | Shared source/observed totals and selected-currency cost                     |
| Model and Deployment Performance | Model token/cost bars                              | 2     | Separate model token and priced-cost comparisons                            |
| Model and Deployment Performance | Backend token/cost scatter                         | 1     | Selected-currency backend scatter, with exact backend table                 |
| Model and Deployment Performance | Model/backend token/cost matrix                    | 1     | Model/backend matrix plus deployment economics table                       |
| Application and Gateway Usage    | UTC date slicer                                    | 1     | Shared reporting window                                                    |
| Application and Gateway Usage    | Token and cost cards                               | 2     | Shared report totals and selected-currency cost                             |
| Application and Gateway Usage    | Application counts by gateway                      | 1     | Explicit distinct observed application counts by gateway                    |
| Application and Gateway Usage    | Region/gateway token/cost matrix                   | 1     | Exact region/gateway matrix                                                |
| Application and Gateway Usage    | Application and region token bars                  | 2     | Application/region comparisons, with exact application/product tables       |
| Peak Usage and Capacity Planning | Date and model slicers                             | 2     | Shared window and attribution filters                                      |
| Peak Usage and Capacity Planning | Token and cost cards                               | 2     | Shared totals and peak recorded hourly measure                              |
| Peak Usage and Capacity Planning | Daily tokens and hour-of-day cost lines            | 2     | Daily demand and separate hour-of-day demand/cost timelines                  |
| Peak Usage and Capacity Planning | Hour-of-day token columns                          | 1     | Hour-of-day distribution and exact values table                            |
| Peak Usage and Capacity Planning | Date/hour token matrix                             | 1     | Keyboard-accessible date/UTC-hour matrix with exact-window investigation     |

Bars show the largest 12 entries when needed; tables and CSV retain every entry.
Heat colours compare the selected measure within one report, not a capacity or
health threshold. Matrix/table cells can drill into matching attribution,
including explicitly unattributed values, or an exact UTC time window.

## Decoded measure semantics and intentional changes

### Product filtering

The reference `GrandTotalTokens` sums source `totalTokens` while removing the
`productName` filter. The portal deliberately keeps every selected attribution
filter effective across totals, trends, tables and exports.

In the controlled fixture, the reference-style grand total is 188 tokens even
when product `p1` is selected. The portal correctly reports 168 tokens for that
selected product. It does not label an all-product total as filtered usage.

### Token prices and currencies

The reference visuals use `CostUSDX`. That calculated column looks up rates by
deployment, then uses prompt/output unit prices or the percentage branch.
Its token branch does not apply the extended cached/audio/image/reasoning rates.
`TotalCostUSD` sums that column without currency separation.

The portal uses reviewed effective-dated, region/model/deployment-aware pricing,
extended token breakdowns and separate monetary totals per currency. It never
applies automatic exchange rates or treats missing/ambiguous prices as zero.

For a fully covered, single-currency fixture with equivalent input/output rates,
the decoded reference arithmetic and portal both yield 185 source tokens and
0.24 USD. Changing the cached-input rate produces 0.2175 USD for the selected
product in the portal instead of the reference's 0.22. A mixed fixture remains
0.22 USD plus 0.02 EUR, not a manufactured 0.24 USD.

### Fixed-period costs

The reference percentage branch distributes BaseCost by the deployment's
prompt-token share across loaded rows. The portal instead requires a declared
`AccountingPeriod`, supported weight configuration and matching trusted
completeness evidence. Partial or ambiguous periods remain unpriced.
See [fixed-period accounting](analytics-data.md#fixed-cost-allocation).

### Missing tokens and counts

The source-total measure is separate from observed prompt plus completion.
Missing source totals make the contributing aggregate unavailable; no subtotal
or token subtype reconstructs that total. Cached, audio, image, reasoning and
prediction counts may overlap parent totals and are never added again.

The portal's count cards are explicitly distinct observed attribution labels,
excluding null labels, not configured APIM inventory. Labels and filters remain
case-sensitive after source normalization.

### Capacity and economics

The model/deployment tab compares usage and estimated costs, not latency or SLOs.
The capacity tab reports recorded UTC hourly demand and peaks. It does not infer
quota, provisioned capacity, saturation, forecasts or availability. Missing
hours are not verified zero demand, and boundary hours may be partial.

## Shared query, version and export contract

New browser reports request `includeAnalysis: true`. All profiles are built
from the same input scan, prices, filters and report checkpoint. Switching tabs,
sorting tables or changing the displayed measure does not query the source.

`dimensionFilters` maps canonical dimensions to an exact label. An omitted key
does not filter; a null value selects unattributed usage. Legacy model/product/
application filter fields remain supported. The shared UTC window is preserved
when attribution filters change, and attribution is preserved when time changes.

Maintained-input drills retain the accepted `readModelVersion`. Refresh data
explicitly unpins it and selects a newer generation. A pinned generation must
belong to the authorized hub/source and remain available; there is no silent
fallback. Source-scan mode cannot provide a transactional snapshot across
separate drill queries, and does not accept a maintained-version pin.

Analytical CSV is bound to the exact completed report and includes profile,
dimension, time, currency, missing-field and version metadata. Each profile is an
independent projection. Do not sum different profiles together; multiple currency
rows repeat usage counts. Missing optional token totals are empty fields, not
zero. Formula-like attribution values are escaped.

Money is calculated/checkpointed on the server with Decimal precision. Graph
coordinates and currency display rounding are presentation approximations.
The server refuses oversized/high-cardinality reports instead of publishing
partial financial totals: 1500 analytical buckets, 1000 primary buckets,
2000 filter-option values and the existing 1.5 MB report-state limit apply.

## Custom attribution privacy

Custom identifiers are disabled in the UI unless the platform config supplies
labels and the analyst has a separate `custom-dimensions` grant in addition to
`finops-data`. For example, the deployment-owned hub JSON can include:

```json
{
  "customDimensionLabels": {
    "custom_dimension_1": "Cost centre",
    "custom_dimension_2": "Business team"
  }
}
```

The analyst must explicitly include custom attribution. Custom reports, history,
resume and CSV recheck that grant; exports additionally require `exports`.
Standard reports do not expose custom identifiers through unpriced labels or
filter options. Raw prompts, request IPs, source document IDs and private
continuation tokens remain outside browser/report exports.

## Remaining acceptance

Local controlled arithmetic, privacy, checkpoint, browser and keyboard/mobile
checks establish the implemented analytical surface and documented alternatives.
They do not certify all real customer Power BI outputs or production throughput.

Customer acceptance must use the same reviewed price history and ingestion
evidence, compare controlled source records in Power BI Desktop and the portal,
and review the intentional differences above. Upstream-pin changes also require
rechecking the PBIR projections and decoded formulas, not only page titles.
See the [completion plan](product-completion-plan.md).
