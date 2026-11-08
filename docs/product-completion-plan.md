---
title: Citadel hosted product completion plan
description: Phased implementation and acceptance gates for trustworthy analytics, dashboard parity, operations and customer delivery.
ms.date: 2026-11-06
---

## Outcome and boundaries

Complete the three-page hosted management product: Gateway Assistant, FinOps
and Operations. FinOps must cover the analytical capabilities of the upstream
Power BI reference, not merely display a few charts. Customer delivery must be
repeatable through the protected GitHub installer and verified against actual
Azure identities, private networks and source data.

The existing App Service UI/BFF, private Container Apps agent, canonical
contract/change engine and single-tenant role/grant model remain the architecture.
Do not replace upstream gateway implementation or bypass its GitOps controls.
All current uncommitted changes must be preserved.

The original completion request authorized implementation and local verification.
Source commit/push is now authorized. Image publication, tenant changes, pricing
changes and Azure deployment remain separate owner-controlled gates.
The latest deployment scope is greenfield APIM/related infrastructure through
pinned upstream Bicep followed by product bootstrap and installation, as defined
in the [customer deployment plan](customer-deployment-plan.md).

## Evidence baseline

- The pinned upstream is `23fbc8fe7f2f`, master 1.0.2/routing 1.0.0.
- The checked upstream branch is `62bec276fa79`, master 1.0.3/routing 1.0.1.
  Its Power BI file is byte-identical to the pinned reference.
- The report definition contains Utilization, Heatmap, Usage and Cost Overview,
  Model and Deployment Performance, Application and Gateway Usage, and Peak
  Usage and Capacity Planning.
- Current UI/source adapters, private-hosting scaffold and installer exist.
  Container acceptance has passed locally with offline cloud fixtures.
- Dashboard completeness, numerical parity and customer Azure acceptance are
  not established by those local tests.

## Milestones

### M1: Resumable complete-source reporting

Status: locally implemented and verified. Production-volume and customer
acceptance claims remain gated by the later milestones.

Replace the browser's latest-N reporting path with durable report runs that
consume source continuation pages until exhaustion. Process bounded batches
per HTTP request and checkpoint aggregates, query identity, pricing snapshot
and source progress with ETags. No source continuation token or customer
connection target may be exposed to the browser.

Persist aggregate state rather than entire source documents. Retain the
requested event-time and attribution filters while scanning mixed upstream
timestamp formats. A completed source scan is not an ingestion watermark:
upstream late arrivals and source mutation remain explicit limitations.

Bind exports to the exact completed report shown in the browser. Preserve
per-user ownership, FinOps/export grants, currency separation, Decimal
arithmetic, missing-token visibility and unpriced usage.

Acceptance:

- A historical event beyond the old 20,000-record boundary is included.
- Retrying or resuming a page does not double-count usage or costs.
- Concurrent advances cannot overwrite checkpoints or publish partial totals.
- Partial progress is visible; incomplete runs cannot export as complete reports.
- Interrupted requests resume without a model, local file state or gateway
  configuration changes.
- Missing prices and source completeness remain explicit, never zero or healthy.
- Browser polling and CSV use the same authorized, immutable completed result.

Local verification:

- The full Python suite passed 896 tests with 23 skips: 11 opt-in live agent
  evaluations and 12 placeholder-template cases. Ruff lint/format passed.
- All 10 UI tests, TypeScript checks and the production frontend build passed.
  Checked reporting Python modules had no Pylance error diagnostics.
- Regression fixtures cover 20,501 source records, opaque/empty feed ranges,
  interrupted checkpoints, ETag/cancellation races, frozen pricing, ownership,
  revoked grants, missing tokens and explicit size/cardinality failures.
- The production UI exercised the real report APIs against 2501 offline source
  records: progress, cancellation, hidden incomplete totals, report-bound CSV
  bytes/Blob and history reopening without rescanning. Identity/source boundaries
  were fixtures, not actual Azure acceptance. Native saved-file download was
  not observable through integrated browser automation.
- Refreshed wheel/sdist checks passed with 288 pinned assets. The standalone
  BFF ZIP imported its own Linux dependency closure and native core offline.
  Local image `citadel-agent:m1-acceptance` passed offline read-only-root
  artifact, compiler and actual Agent/BFF HTTP acceptance.
- Publication now includes the report/analytics/client regression slice;
  the changed publication workflow passed actionlint.

Runs are client-advanced and retained for one day. They have explicit
aggregate/filter/state-size limits, not a total source-record cap. Maintained
incremental processing, ingestion completeness and fixed-period allocation
are not completed by M1.

### M2: Incremental analytics and fixed-period accounting

Status: locally implemented and verified. Customer pipeline assertions,
production-volume sizing and live acceptance remain owner/customer-gated.

Add a maintained analytical read model with indexed UTC event time, hourly/daily
rollups, durable ingestion checkpoints, correction/replay support and bounded
retention. Separate ingestion scan progress from authoritative event-time
completeness. Backfill, late events, source updates and deletes must have
defined handling and tests; never infer pipeline completeness from the newest
event or the current wall clock.

Integrate reviewed effective-dated pricing and fixed-cost/PTU allocation for a
complete, explicitly defined accounting period. Price changes invalidate or
version derived results. Quarantine metadata must not contain raw prompts,
credentials or unnecessary personal dimensions.

Acceptance:

- Over 100,000 fixture events and multiple reporting periods reconcile exactly.
- Incremental/replayed/corrected inputs match a clean rebuild.
- Hourly/daily rollups and exports reconcile by every supported dimension.
- PTU allocation across a complete period sums exactly to its reviewed BaseCost.
- Incomplete/ambiguous periods stay unpriced.
- Scheduled processing, lease ownership, retention and RBAC are wired through
  the hosted runtime and infrastructure, not left as unused libraries.

Implementation:

- Immutable scope-bound analytics generations contain pseudonymous normalized
  observations, reviewed prices, exact event allocations and full-dimensional
  hourly/daily buckets. A single published head prevents mixed-version queries.
- Source-write-time delta polling uses overlapping `_ts` boundaries for creates
  and corrections. Daily empty-generation reconciliation removes hard deletes.
  This is an intentional alternative to change-feed consumption because the
  existing source feed/backup mode cannot be changed by this request and default
  latest-version change feed omits hard deletes.
- A dedicated private scheduled worker and analytics container are wired into the
  image, runtime and Bicep. The worker cannot write source data or access browser
  sessions, model credentials or Key Vault secrets.
- Maintained reports merge complete hourly interiors and exact event-time edges.
  They pin one immutable version. The browser offers an explicit source scan
  rather than silently falling back when analytics are unavailable.
- Percentage pricing requires a reviewed `AccountingPeriod` and exactly matching
  trusted producer facts. Missing/ambiguous evidence keeps PTU unpriced. No
  completeness record, gateway change or customer pricing was created.

Local verification:

- The 100,001-event stress case reconciled two fixed-cost periods plus token
  pricing, all 12 attribution dimensions and report-bound CSV totals exactly.
- All remaining Python tests passed: 914 passed/23 skipped. The stress case
  passed separately, giving 915 verified passing tests overall. Its longer run
  exposed import-time session expiry in test fixtures; fixture clocks/session
  creation were corrected without changing production authentication lifetimes.
- All 11 UI tests, production build, TypeScript and Ruff lint/format passed.
  Checked new Python modules had no Pylance error diagnostics; the service
  transport became unavailable for later checks.
- Bicep and compiled-property tests passed with no schema warnings. The job
  retains supported API `2025-01-01` because the release-pinned compiler cannot
  type-check the newer catalog API. Offline Checkov 3.4.0 passed 15 checks over
  50 resource instances, with no failures, skips or parsing errors.
- A real browser with production UI/report APIs and synthetic source/identity
  verified 1101 requests, a 1000 USD fixed-period allocation, version-bound CSV
  and explicit source selection retaining unpriced fixed-period costs.
- Refreshed wheel/sdist, portable Linux BFF ZIP and local
  `citadel-agent:m2-acceptance` image passed packaging/import and offline
  artifact/compiler/Agent/BFF HTTP acceptance. Publication actionlint passed.

Operational limits remain explicit: refresh copies retained observations and
rederives rollups, hard deletes lag until full reconciliation finishes, and
generations have retention/write/cardinality guards. These tests do not prove
production RU/s, refresh latency or actual customer ingestion completeness.
See [analytics semantics](analytics-data.md) for the producer proof contract,
retention, failure behavior and sizing limits.

### M3: Power BI analytical parity in the FinOps page

Status: locally implemented and verified, with documented deliberate measure
differences. Live customer Power BI reconciliation and volume acceptance remain gated.

Keep three top-level product pages. Add FinOps tabs for all six reference
analytical views:

| View | Required capability |
| --- | --- |
| Utilization | Extended token composition, priced/unpriced usage and usage/cost breakdowns |
| Heatmap | Product/model consumption and cost treemaps or accessible equivalent heatmaps |
| Usage and Cost Overview | Hourly/daily trends, aligned filters and comparable totals |
| Model and Deployment Performance | Backend/model comparisons, matrices and scatter analysis |
| Application and Gateway Usage | Application, product, region and gateway attribution |
| Peak Usage and Capacity Planning | Hour-of-day distributions, peaks and capacity investigation |

Expose custom dimensions with administrator-defined labels and privacy-aware
grants. Add shared filters, sorting, drill-through, accessible chart tables,
empty/error/stale states and exportable breakdowns.

Acceptance:

- A traceability matrix maps every upstream analytical visual/measure to its
  portal equivalent or a documented intentional alternative.
- UTC boundaries, same-source comparisons, missing telemetry and multiple
  currencies have deterministic test fixtures.
- A controlled dataset reconciles portal outputs with independently calculated
  values and the Power BI model; every deliberate pricing difference is recorded.
- Desktop/mobile/keyboard/browser acceptance covers all six analytical views.

Implementation and local evidence:

- The actual pinned PBIR contains six pages and 44 data-bound visuals. The
  decoded `GrandTotalTokens` clears product filtering, while visual `CostUSDX`
  uses the simpler deployment-based prompt/output or percentage formula.
  The [traceability matrix](finops-parity.md) maps every bound visual and records
  the intentional filtering, extended-pricing, currency and completeness changes.
- All six tabs share a completed report's compact projections. Shared filters
  support every canonical dimension and explicit unattributed values. Maintained
  drills pin the accepted generation until explicit Refresh data; tabs and table
  sorting do not query sources.
- Utilization exposes extended token completeness; Heatmap uses accessible
  proportional/heat tiles and exact tables; Overview has hourly/daily series;
  Performance has model/backend matrices and currency-separated scatter;
  Application/Gateway has attribution and gated custom labels; Capacity has
  UTC hour distributions and a date/hour investigation matrix. No forecast,
  saturation or SLO is inferred.
- Controlled fixtures reconcile quantities, currencies, exact CSV profiles,
  mixed timestamp formats, midnight boundaries, retry checkpoints and version-
  pinned drills. Bucket limits fail explicitly. Custom attribution requires
  configured labels, explicit inclusion and a separately rechecked grant.
- 926 Python tests passed/23 skipped in the full run; the 100,001-event stress
  case passed separately after updating its fixture to request the new required
  custom authorization. Overall, 927 passing tests were verified.
- All 19 UI tests, TypeScript, production build and Ruff lint/format passed.
  Checked reporting Python modules had no Pylance error diagnostics.
- Production UI plus real report APIs with synthetic identity/source passed
  all-six-view desktop, keyboard and 375px mobile checks, exact report CSV,
  separated currencies and gated custom inclusion. Actual mobile testing found
  and fixed a Fluent tab-wrap override and selected-view loss during async
  refresh. Native saved-file download and real Entra sign-in remain customer checks.
- Refreshed wheel/sdist, portable Linux BFF ZIP and
  `citadel-agent:m3-acceptance` passed package-shape/exact-content and offline
  artifact/compiler/Agent/BFF HTTP acceptance.

This completes the implemented six-view surface, not an assertion that every
real Power BI number is identical. Follow the documented differences and live
acceptance requirements in [FinOps parity](finops-parity.md).

### M4: Operational investigation and telemetry health

Status: locally implemented. Customer source permissions, diagnostic settings,
pipeline configuration and real-data acceptance remain M6 gates.

Add backend/model/region/application drilldowns, correlation-ID investigation,
active-alert/incident views and safe links to diagnostics. Add ingestion-pipeline
health for Event Hub, processing and Cosmos, with evidence of freshness.
Distinguish no traffic, missing permission, delayed ingestion and outages.

Add circuit-breaker/failover/cache signals only when the source actually
provides them. Availability and SLO/error-budget claims require real probes,
configured targets and trustworthy time windows, not a relabelled HTTP success rate.

Acceptance:

- Test healthy, delayed, denied, empty and failed telemetry independently.
- Never infer availability from zero requests or average precomputed percentiles.
- Correlation lookups and alert actions recheck grants and redact sensitive fields.
- Operators can move from a bad signal to scoped evidence and a reviewed draft.

Implementation and local evidence:

- Resource-scoped gateway filters use real APIM identifiers. Model/application
  attribution is a separate generation-pinned nonfinancial count, not an
  invented join to gateway latency or response-code data.
- GUID/time-bounded request evidence has a fixed redacted projection and an
  additional raw-log grant. Active alert instances use read-only, resource-
  scoped ARM requests without context or arbitrary next-link following.
- Optional deployment-owned component IDs expose supported count/total metrics,
  freshness, denied/empty/stale/error states and analytics quarantine evidence.
  Zero traffic is not healthy and fresh telemetry is not ingestion completeness.
- The production UI and real API routes with synthetic identity/sources passed
  desktop evidence lookup, all five investigation tabs and an unsent assistant
  handoff. All five tabs fit a 375px viewport without document overflow. Actual
  queued-turn submission and cancellation passed without a model call.
- Source tests cover invalid scope/IDs, safe KQL literals, missing/denied/partial
  sources, redaction, alert-target validation, count-only attribution and
  supported-baseline checks. These are offline checks, not customer telemetry
  or Entra acceptance.

### M5: Agent reliability, governance and supported upstream surface

Status: local reliability and release-preflight implementation complete;
actual production governance remains blocked/unknown until owner verification.

Move replica-local long-running work to a durable execution/lease mechanism
where necessary. Define interruption, retry, cancellation and uncertain-effect
handling; no automatic replay of PR/deployment/key-rotation side effects.

Productize drift checking and close prioritized conversational contract gaps
without exposing arbitrary template parameters. Review the upstream 1.0.3/
routing 1.0.1 changes and either update/test the pin or explicitly retain the
supported baseline. Reuse existing sync and validation tools.

Acceptance:

- Worker/restart/concurrency tests preserve effective user, hub scope and audit.
- Approvals remain hash-bound; production needs an independent approver.
- Drift reports distinguish real difference, known noise, unknown and failure.
- Supported capability documentation matches schema, tools, rendering and tests.
- Production reviewers, OIDC scopes and branch/environment protections are
  selected and actually verified, not described as enforced merely by a template.

Implementation:

- New hosting uses an atomic per-conversation durable queue and a private
  scheduled assistant job. ETag claims, independently renewed worker leases,
  current session/grant checks, cancellation and explicit execution-start
  checkpoints prevent stale writes and automatic started-work replay.
- Disabled persisted grants override bootstrap grants. Older prepared hosts
  retain explicit replica-local compatibility until the worker is deployed.
- Uncertain or stale PR execution requires review; it cannot reset to prepared
  or be automatically retried. Hash-bound independent production approval and
  PR-only hosted execution remain intact.
- Operations exposes the existing schema/catalog drift checker and known-noise
  classification. The supported upstream pin is deliberately retained rather
  than claiming untested 1.0.3/routing 1.0.1 support or live deployment drift.
- A read-only release checker verifies exact source/artifact identity and
  actual GitHub protection metadata. Current source is dirty and artifacts are
  unpublished; remote protection metadata was not retrievable. These gates stay
  blocked/unknown. No reviewers, expected checks, policies or approvals were
  invented or changed.

Local pre-live verification:

- The final full Python run passed 956 cases with 23 explained skips in
  714.41 seconds. It includes the 100,001-event reconciliation case and all
  worker/authorization/lifespan tests against the final source after cleanup.
  The focused Operations/worker/release/infrastructure/installation run also
  passed 55 cases.
- All 23 UI tests, TypeScript, production build, Ruff lint/format and actionlint
  passed. The 23 Python skips are 11 opt-in live evaluations and 12 intentional
  Bicep-placeholder template cases, not unreported local test failures.
- Bicep compiled locally. Offline Checkov 3.4.0 passed 15 checks over 51 resource
  instances, with zero failures, skips or parsing errors. Azure provider
  validation/what-if and deployment were not run.
- Current wheel/sdist shape checks passed with 288 pinned assets. A Git-free,
  offline sdist rebuild also passed. The local pre-live image passed non-root
  UID 10001, read-only-root/no-network artifact/compiler and real Agent/BFF
  HTTP acceptance, including unconfigured assistant-worker refusal.
- The refreshed portable BFF ZIP matched all 425 wheel package files exactly.
  Linux imports used only the ZIP's own dependency closure plus the standard
  library, without source-tree or installed-image fallback. The unchanged
  runtime lock was checked against the previously verified portable closure.
  Fresh public dependency restoration and reviewed publication remain CI gates.
- A second acceptance created an empty Linux virtual environment, confirmed
  missing dependencies, restored the hash-locked runtime without cache or
  credentials through the configured anonymous mirror, imported the packaged
  worker/server and passed `pip check`. Direct PyPI metadata matched sampled
  locked wheel hashes, but this network rejected TLS handshakes to
  `files.pythonhosted.org`. TLS verification was not disabled; direct-public
  package delivery still needs verification on the clean publication runner.
- A fresh local release check remained blocked/unknown for dirty source,
  unpublished/unbound artifacts and unverified remote protections. No local
  manifest or cached CLI account was treated as approval.

### M6: Customer release and live acceptance

Status: owner/customer-gated; preparation can run in parallel.

Review and publish the source/workflows, publish the tested immutable public
image, supply the dedicated Linux App Service and completed installation JSON,
prepare Entra roles/scopes/BFF app-role assignment, Key Vault references and
protected GitHub environment.

Run the real installer preview, review costs/quotas/private connectivity and
what-if, then obtain explicit deployment approval. Complete actual sign-in,
data/model access, dashboard reconciliation, controlled PR-only change,
backup/restore, load and rollback acceptance.

Acceptance:

- Fresh CI and artifact provenance refer to the exact reviewed release source.
- Tenant, network, grants and data accuracy are verified from a real browser.
- No placeholder reviewers, configuration targets or prices reach production.
- Publish a tested support matrix, limitations and recovery procedure.
- Version bump/tag/publication happen only after the release owner approves.

## Additional enhancements, not assumed upstream parity

Actual invoice reconciliation, forecasting, budgets, optimization suggestions,
automatic exchange rates, broader non-LLM analytics and cross-hub reporting are
separate extensions. Define their financial/source semantics before claiming
support. Hub/model creation and gateway-upgrade orchestration remain explicit
scope decisions; the hosting installer does not provision a gateway.

## Implementation order and definition of done

Start with M1 as an end-to-end vertical slice: persistent aggregate checkpoints,
authorized run APIs, browser progress/resume, report-bound export, tests and docs.
Then implement M2 and M3 without presenting a bounded preview as complete
financial reporting. M4 and M5 can progress against the shared contracts while
M6 prerequisites are arranged.

Every milestone requires implementation, targeted regression tests, relevant
build/type/lint checks, directly related documentation and evidence recorded in
the ignored local backlog. Do not mark the entire product complete when only
an individual milestone or local fixture test is done.
