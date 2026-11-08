---
title: Portal analytics data semantics
description: Source normalization, token pricing, fixed-cost allocation, and data quality rules for portal analytics
---

## Scope

The analytics package defines shared data semantics for the hosted FinOps
dashboard and API. The portal now queries customer usage and pricing containers,
normalizes records and exposes scoped dashboard and CSV export endpoints.
The browser uses resumable report runs over either a maintained immutable
read-model generation or an explicitly selected complete-source scan.
These rules prevent missing telemetry or pricing from being treated as zero.

## Usage source

The upstream gateway writes one request per document to
`llm-usage-container`. The normalized event records a trusted `HubScope`
provided by server-side configuration; it never accepts tenant or hub scope
from the usage document. Its stable event ID is a SHA-256 hash of the Cosmos
account, database, container, canonical partition-key JSON, and document ID.

`promptTokens` and `responseTokens` are required non-negative counts.
Optional source counts include cached, audio, reasoning, prediction, image, and
total tokens. Missing optional counts are represented as zero for arithmetic,
but `available_usage_fields` preserves whether the source actually supplied
each value. Total tokens are never inferred. Subtype counts larger than their
corresponding prompt or completion total are rejected.

The upstream Power BI query converts UTC Kusto `TimeGenerated` values to a
timezone-less `M/dd/yyyy h:mm:ss tt` display string. That exact documented
format is interpreted as UTC. Other timezone-less timestamps are rejected;
ISO timestamps must include an offset or `Z`.

Cached, audio, reasoning, and image counts can be subsets of prompt or
completion totals, so they must not be added to those totals. If a pricing
breakdown cannot distinguish overlapping subtypes, the event remains
unpriced rather than receiving a guessed amount.

## Token pricing

Token-based prices use `Decimal` arithmetic and must match the event's model or
deployment, region, and effective interval. A region-specific match takes
precedence over `ALL`. Overlapping entries at the same precedence are
ambiguous. Retired entries can price events within their historical interval,
but an active entry without an effective start date is not applied to
historical events.

Upstream `model-pricing` entries currently have no effective dates. Add
reviewed effective dates before using those rates for historical reporting.
Price values should be reviewed and seeded by the platform owner; the agent
does not invent provider prices.

## Fixed-cost allocation

Upstream `percentage` entries distribute `BaseCost` across usage events for a
complete reporting period. Exactly one unit-cost field must be `1` and every
other unit-cost field must be `0`; the selected field defines the usage weight.
The allocator requires all events for one hub scope and matching deployment
inside the half-open reporting window, a source completeness watermark through
the window end, and a price effective for the entire window. Allocations use
the event's share of the selected weight and sum exactly to `BaseCost`.

Cached-audio weight is not supported because the source does not expose a
separate cached-audio token count. Missing weight telemetry, zero total weight,
an incomplete period, or a price that does not cover the full period produces
an explicit unpriced result. It does not distribute fixed costs across a
partial month.

Maintained reports additionally require a reviewed `AccountingPeriod` on each
percentage price. This half-open interval defines what its `BaseCost` pays for;
the reporting window or price effective interval does not invent a billing
period. A query for part of that interval reads its previously allocated costs,
not another allocation of the entire fixed charge.

The worker distributes costs at 28 fractional digits, rounding weighted shares
down and placing the exact remainder on the lexicographically last positive-
weight event ID. Hourly rollups, daily rollups and stored event allocations sum
exactly to the reviewed BaseCost. Missing deployment, ambiguous rates, missing
weight fields, zero total weight or unsupported precision remain unpriced.

### Trusted pipeline completeness

`completenessContainer` optionally names an existing producer-controlled
container in the usage database. The installer does not create assertions or
modify the upstream ingestion pipeline. Neither browser users nor the analytics
worker have write permission on that database.

The producer's version-1 record must contain:

| Field                  | Required evidence                                               |
| ---------------------- | --------------------------------------------------------------- |
| `scope`                | Exact configured tenant, subscription, hub and environment      |
| `sourceFingerprint`    | Versioned hash of the configured source and completeness target |
| `pricingFingerprint`   | Hash of the reviewed captured pricing entries                   |
| `window`               | Exact half-open accounting period                               |
| `completeThrough`      | The declared period's end                                       |
| `issuedAt`             | Assertion time, no earlier than the period end                  |
| `sourceWriteCutoff`     | Latest Cosmos source-write boundary included by the assertion   |
| `eventCount`           | All normalized events in the period, before dashboard filters   |
| `eventDigest`          | Order-independent digest of the same normalized accounting facts|

The digest is the sum modulo 2^256 of SHA-256 values of canonical normalized
observations, excluding ingestion time and warning codes and sorting available
token-field names. Source IDs are already pseudonymized in those observations.
Producer code must use the shared versioned fingerprint/digest functions and
assert real pipeline completeness; matching a source scan alone is insufficient.

Exactly one matching assertion is required. It must match the generation's
event count, digest and pricing fingerprint, with a source-write cutoff no later
than the captured source boundary. Missing, duplicated, stale-version or
contradictory assertions keep PTU costs unpriced. Any quarantined source record
also prevents fixed-period pricing. Price edits or later event corrections
invalidate old assertions. There is no operator-close endpoint.

## FinOps aggregation

The rollup builder emits UTC hourly or daily buckets, clipped to the requested
half-open time window. Its default dimensions are model, deployment, product,
application, backend, and region; callers can select a different dimension
set or request an overall total. Monetary amounts retain Decimal precision
through aggregation, with up to 28 fractional digits; presentation layers
should apply currency-specific display rounding.

Pricing coverage is weighted by prompt plus completion tokens. Unpriced
events remain visible through their dimensions and pricing warning codes.
Optional token counts that are absent from any event add a quality warning,
and aggregate `totalTokens` remains unknown unless every event supplied it.
The builder is deterministic. Its incremental accumulator serializes money,
token-completeness flags and pricing lineage without losing Decimal precision.
Per-report checkpoints persist this state atomically with input progress.
The maintained model stores full-dimensional hourly/daily buckets and normalized
event facts separately. Report interiors merge complete hourly buckets; partial
hour boundaries read exact event timestamps. Token and financial totals are
never prorated from a coarser bucket.

## Maintained read-model lifecycle

A private scheduled Container Apps job runs the image's `analytics` entrypoint
every five minutes. It has its own managed identity, read-only usage access and
write access only to the application-owned analytics container. It needs no
Entra client secret, session state, GitHub credential or model access.

Each generation contains pseudonymous normalized observations, reviewed prices,
allocation results, hourly/daily rollups and a durable control record. Data rows
and processing progress commit in one conditional Cosmos transaction in the
same scope/generation partition. ETags fence expired/competing workers, including
lost-acknowledgement retries. A head pointer publishes only a finished immutable
generation. Readers never see its in-progress rows.

Incremental refresh copies the previous immutable observations, applies source
creates/updates since the preceding `_ts` cutoff with a one-second overlap,
then rebuilds derived costs/rollups against captured prices. Repeated source
IDs replace their prior observation rather than adding another request. Repairs
and timestamp/dimension changes remove old contributions. Invalid updates
replace old observations with quarantined reason codes, not raw error payloads.

This is source-write-time delta polling, not a Cosmos change-feed processor.
Default Cosmos latest-version change feed does not expose hard deletes.
The worker therefore starts an empty full-reconciliation generation at least
daily, removing deleted source IDs when that generation finishes. A privileged
worker invocation can also request `analytics --force-full`. Deletions may
remain visible until the next full reconciliation completes; the response marks
this delay explicitly. No existing usage account backup/feed mode is changed.

Default event-time retention is 93 days. Generation data expires after three
days; incomplete expired generations are discarded rather than published.
Source backfills within retained event time are picked up by their newer write
time. Older windows require the explicitly selected source-scan report.
Quarantine rows retain only a hash identity and reason, not source IDs,
partitions, prompts, addresses or malformed field values.

Worker source pages contain at most 20 records and each transaction is guarded
below 100 operations and 1 MB. A generation has a 2 GB cumulative write budget.
Capacity failures are explicit and preserve the last published generation.
The worker yields after about four minutes between pages and releases its
lease; individual Azure calls can take longer. Prices and completeness records
are each limited to 2000 scanned entries, with explicit warning/unpriced behavior.
At most 24 complete accounting/proof windows are evaluated per generation.

Refresh currently copies retained observations and rederives every aggregate.
It avoids rescanning old source documents between full reconciliations, but is
not a constant-cost incremental rollup updater. The 100k-plus fixture proves
numerical/replay correctness, not production RU/s, throughput or latency.
Review partition size, storage/RU budget and refresh lag during live acceptance.

## Dashboard integration status

New browser reports request all six analytical projections. Their compact
checkpoints contain single-window attribution totals, matrices and hourly/daily
series, not raw documents. All projections share one captured input and price
version. Tab/measure changes and table sorting are local; attribution/time drills
start another authorized report pinned to the same maintained generation.
Refresh data explicitly selects a newer generation. A source-scan drill remains
a fresh non-transactional source query.

`dimensionFilters` accepts every canonical attribution dimension, with null
meaning explicitly unattributed rather than no filter. Custom dimensions require
administrator labels and the separate `custom-dimensions` grant; standard
reports do not expose their values through filter options or unpriced labels.
See [six-view traceability and privacy](finops-parity.md).

The browser starts a per-user report, then advances it in bounded batches.
The explicit source-scan mode consumes up to four 500-record pages across SDK-provided opaque
Cosmos feed ranges. State includes the query, frozen reviewed-price snapshot,
aggregate checkpoint and private continuation; ETags prevent competing requests
from committing the same checkpoint twice. A source-read failure resumes from
the last committed page. Closing a tab pauses further advancement; report history
can reopen or resume the run.

The source query fixes its Cosmos `_ts` upper bound at report creation. Mixed
upstream event timestamps are normalized before the requested event-time and
attribution filters are applied. There is no latest-20,000 source-record cap
on this browser path. Empty pages with continuations and all opaque feed ranges
must be exhausted before a result is published.

`sourceScanComplete` means that the run consumed its selected input pages. It is
not proof that ingestion is complete, or a transactional snapshot across source
updates, deletes and late arrivals. No ingestion watermark is fabricated.
Only the maintained mode can use evidence-gated fixed-period allocations.
The direct source-scan mode keeps percentage prices unpriced.

With analytics configured, the page defaults to the latest published generation.
Its report pins that version and captured prices, even if a newer generation
appears. Expired versions fail explicitly; unavailable analytics never silently
falls back to an unbounded customer-source read. Select the source-scan input
explicitly when needed. A refresh older than 30 minutes is stale, and a query
ending after captured coverage remains partial.

Reports are held for one day in application-owned state. Only normalized
aggregate metadata is stored, not raw usage documents, request bodies or IP
addresses. Limits of 1000 primary buckets, 1500 analytical buckets,
2000 filter-option values and the
1.5 MB product-state limit cause an explicit failed report, not success-shaped
partial financial totals. The reporting window remains limited to 93 days.

CSV export references the exact completed report ID, not a new source query.
It includes `input_kind` and `read_model_version`, requires both FinOps and export
grants and repeats ownership checks. Formula
prefixes are escaped. No raw request bodies, IP addresses or conversation
contents are exported.

Analytical CSV adds profile, full dimension, extended token and missing-field
columns. Profiles are independent and must not be summed together; currency
rows repeat usage counts. Missing optional token values are empty fields.
The older CSV shape remains for reports/previews without analytical projections.

The legacy `/finops/query` and `/finops/export` routes remain bounded live
previews for compatibility. Their responses explicitly identify that mode and
do not claim a completed source scan. The assistant's quick usage tool also
uses the bounded preview; it is not a maintained accounting report.

Signed cursor functions remain library-level building blocks. Scoped page queries
bind cursors to the authorized hub scope, Cosmos account/database/container,
normalized SQL parameters, page size, and expiry. Quality assessment reports
an unknown state without a watermark, a partial state for incomplete windows
or quarantined records, and a stale state when the caller-supplied freshness
limit is exceeded.
Cursor tokens are signed, not encrypted. Treat them as opaque. The signer
requires a key of at least 32 bytes; a hosted deployment must load it from a
secret store and use the same key across app instances. Rotating the key
invalidates outstanding cursors. Report continuations and routing ranges remain
private in durable state; the browser receives report IDs and progress only.
Cosmos SDK page iteration uses `PageIterator.continuation_token`, not an
unsupported query-result response-header method. Customers must connect real
pipeline completeness evidence before claiming complete accounting periods.
Actual invoice reconciliation and true change-feed/deletion-log integration
remain extensions, not features implied by the local reporting tests.

See the upstream [Power BI dashboard guide](../upstream/ai-hub-gateway/guides/power-bi-dashboard.md)
for the source container and pricing-entry schema.
See the [completion plan](product-completion-plan.md) for the remaining milestones.
