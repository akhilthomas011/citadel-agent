import { useMemo, useRef, useState } from "react";
import { Button, Checkbox, Field, Input, Select } from "@fluentui/react-components";
import { PortalApi } from "./api";
import { count, LoadState, money, Notice, QualityNote, Stat, Trend, useLoad } from "./components";
import type { Dimension, FinOps, FinOpsReport, Portal, Query } from "./types";
import { DIMENSION_LABELS, FinOpsViews } from "./FinOpsViews";
import type { FinOpsView } from "./FinOpsViews";
import { OperationsInvestigation } from "./OperationsInvestigation";
import type { GatewayDimension, OperationsQuery } from "./types";

const dimensions: Dimension[] = ["model", "deployment", "product", "application", "backend", "region",
  "gateway", "service_type", "operation_name", "ai_gateway_id"];
const customDimensions: Dimension[] = ["custom_dimension_1", "custom_dimension_2"];

export function initialQuery(days = 7): Query {
  const end = new Date();
  return { window: { start: new Date(end.getTime() - days * 86400000).toISOString(), end: end.toISOString() }, groupBy: "model" };
}

function TimeFilter({ apply }: { apply: (query: Query) => void }) {
  const [period, setPeriod] = useState("7");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [error, setError] = useState("");
  function update() {
    if (period !== "custom") { setError(""); apply(initialQuery(Number(period))); return; }
    const a = Date.parse(`${start}:00Z`), b = Date.parse(`${end}:00Z`);
    if (!Number.isFinite(a) || !Number.isFinite(b) || b <= a || b - a > 93 * 86400000 || b > Date.now() + 300000) {
      setError("Choose an ordered UTC window of at most 93 days, ending no later than now."); return;
    }
    setError(""); apply({ window: { start: new Date(a).toISOString(), end: new Date(b).toISOString() }, groupBy: "model" });
  }
  return <div className="time-filter">
    <Field label="Reporting window"><Select value={period} onChange={(_, data) => setPeriod(data.value)}>
      <option value="1">Last 24 hours</option><option value="7">Last 7 days</option>
      <option value="30">Last 30 days</option><option value="custom">Custom UTC window</option>
    </Select></Field>
    {period === "custom" && <>
      <Field label="From (UTC)"><Input type="datetime-local" value={start} onChange={(_, data) => setStart(data.value)} /></Field>
      <Field label="Until, exclusive (UTC)"><Input type="datetime-local" value={end} onChange={(_, data) => setEnd(data.value)} /></Field>
    </>}
    <Button appearance="primary" onClick={update}>Apply window</Button>
    {error && <span role="alert" className="field-error">{error}</span>}
  </div>;
}

export function FinOpsPage({ api, portal }: { api: PortalApi; portal: Portal }) {
  const [query, setQuery] = useState<Query>(() => initialQuery());
  const [currency, setCurrency] = useState("");
  const [exportError, setExportError] = useState("");
  const [exporting, setExporting] = useState(false);
  const [selectedReport, setSelectedReport] = useState<string>();
  const [active, setActive] = useState<FinOpsReport>();
  const [view, setView] = useState<FinOpsView>("utilization");
  const pinnedVersion = useRef<string | undefined>(undefined);
  const [includeCustom, setIncludeCustom] = useState(false);
  const [inputKind, setInputKind] = useState<"source-scan" | "read-model">(
    portal.analyticsEnabled ? "read-model" : "source-scan"
  );
  const recent = useLoad((signal) => api.recentReports(signal), [api]);
  const report = useLoad((signal) => api.finops(query, signal, (run) => {
    if (!signal.aborted) {
      setActive(run);
      if (run.inputKind === "read-model" && run.readModelVersion) pinnedVersion.current = run.readModelVersion;
    }
  }, selectedReport, inputKind, includeCustom, pinnedVersion.current), [api, JSON.stringify(query), selectedReport, inputKind, includeCustom]);
  const data = report.value;
  const selectedCurrency = data?.costs.some((cost) => cost.currency === currency) ? currency : data?.costs[0]?.currency;
  const selectedCost = data?.costs.find((cost) => cost.currency === selectedCurrency);
  const trend = useMemo(() => {
    const days = new Map<string, number>();
    for (const row of data?.aggregates ?? []) {
      const day = row.window.start.slice(0, 10);
      days.set(day, (days.get(day) ?? 0) + row.usage.prompt + row.usage.completion);
    }
    return [...days].sort(([a], [b]) => a.localeCompare(b)).map(([label, value]) => ({ label, value }));
  }, [data]);
  const costTrend = useMemo(() => {
    const days = new Map<string, number>();
    for (const row of data?.aggregates ?? []) {
      const cost = row.costs.find((entry) => entry.currency === selectedCurrency);
      if (!cost) continue;
      const day = row.window.start.slice(0, 10);
      days.set(day, (days.get(day) ?? 0) + Number(cost.amount));
    }
    return [...days].sort(([a], [b]) => a.localeCompare(b)).map(([label, value]) => ({ label, value }));
  }, [data, selectedCurrency]);
  async function download() {
    setExporting(true); setExportError("");
    try {
      if (!data?.reportId || !data.sourceScanComplete) throw new Error("Complete a report before exporting.");
      const result = await api.exportFinops(data.reportId);
      const url = URL.createObjectURL(new Blob([result.content], { type: "text/csv;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url; link.download = result.filename; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (reason) { setExportError(reason instanceof Error ? reason.message : "Export failed."); }
    finally { setExporting(false); }
  }
  const labels = { ...DIMENSION_LABELS, ...portal.customDimensionLabels };
  const enabledDimensions = [...dimensions, ...(includeCustom ? customDimensions.filter(name => portal.customDimensionLabels?.[name]) : [])];
  function filter(name: Dimension, value: string | null | undefined) {
    const filters = { ...query.dimensionFilters };
    if (value === undefined) delete filters[name]; else filters[name] = value;
    applyQuery({ ...query, ...(name === "model" || name === "product" || name === "application" ? { [name]: undefined } : {}), dimensionFilters: filters });
  }
  function drill(filters: Partial<Record<Dimension, string | null>>, window?: Query["window"]) {
    applyQuery({ ...query, dimensionFilters: { ...query.dimensionFilters, ...filters },
      ...(window ? { window } : {}) });
  }
  function applyQuery(next: Query) {
    setSelectedReport(undefined); setActive(undefined); setQuery(next);
  }
  function refresh() {
    pinnedVersion.current = undefined;
    setSelectedReport(undefined); setActive(undefined); report.refresh(); recent.refresh();
  }
  async function cancel() {
    if (!active) return;
    try { setActive(await api.cancelReport(active.id)); recent.refresh(); }
    catch (reason) { setExportError(reason instanceof Error ? reason.message : "Report cancellation failed."); }
  }
  return <>
    <div className="page-toolbar"><TimeFilter apply={(next) => applyQuery({ ...query, window: next.window })} />
      <div className="inline"><Button onClick={refresh} disabled={report.loading}>Refresh data</Button>
        {portal.allowedActions.includes("export.create") && <Button disabled={exporting || !data?.sourceScanComplete || report.loading} onClick={download}>Export CSV</Button>}</div>
    </div>
    <p className="window-caption">UTC reporting window: {query.window.start} to {query.window.end} (end exclusive).</p>
    {portal.analyticsEnabled && <Field label="Report data input"><Select value={inputKind} onChange={(_, value) => {
      pinnedVersion.current = undefined;
      setInputKind(value.value === "read-model" ? "read-model" : "source-scan");
      setSelectedReport(undefined); setActive(undefined);
    }}>
      <option value="read-model">Maintained analytics (scheduled refresh)</option>
      <option value="source-scan">On-demand source scan</option>
    </Select></Field>}
    {portal.analyticsEnabled && <p className="muted">Maintained analytics uses the latest published generation. If a refresh is not ready or your window precedes retention, select an on-demand source scan. Missing completeness evidence keeps fixed-period costs unpriced.</p>}
    {Object.keys(portal.customDimensionLabels ?? {}).length > 0 && <Checkbox checked={includeCustom}
      label="Include sensitive custom attribution" onChange={(_, value) => {
        const enabled = value.checked === true;
        setIncludeCustom(enabled);
        const filters = { ...query.dimensionFilters };
        if (!enabled) customDimensions.forEach(name => delete filters[name]);
        applyQuery({ ...query, dimensionFilters: filters,
          groupBy: !enabled && customDimensions.includes(query.groupBy) ? "model" : query.groupBy });
      }} />}
    {includeCustom && <Notice>Custom attribution can contain personal identifiers. It requires administrator labels and your separate sensitive-data grant, including for report history and exports.</Notice>}
    <div className="filter-bar">
      {!data?.analysis && active?.includeAnalysis !== true && <Field label="Group costs by"><Select value={query.groupBy} onChange={(_, value) => {
        const selected = enabledDimensions.find((item) => item === value.value);
        if (selected) applyQuery({ ...query, groupBy: selected });
      }}>{enabledDimensions.map((dimension) => <option key={dimension} value={dimension}>{labels[dimension]}</option>)}</Select></Field>}
      {enabledDimensions.map((name) => <Field key={name} label={`Filter ${labels[name]}`}>
        <Select value={name in (query.dimensionFilters ?? {}) ? (query.dimensionFilters?.[name] === null ? "m:" : `v:${query.dimensionFilters?.[name]}`) :
          (name === "model" || name === "product" || name === "application") && query[name] ? `v:${query[name]}` : ""}
          onChange={(_, value) => filter(name, value.value === "m:" ? null : value.value.startsWith("v:") ? value.value.slice(2) : undefined)}>
          <option value="">All {labels[name]}</option><option value="m:">Unattributed</option>
          {(data?.filterOptions?.[name] ?? (name === "model" ? data?.models : name === "product" ? data?.products : name === "application" ? data?.applications : []) ?? [])
            .map((value) => <option key={value} value={`v:${value}`}>{value}</option>)}
        </Select></Field>)}
      {data && data.costs.length > 1 && <Field label="Display currency"><Select value={selectedCurrency}
        onChange={(_, value) => setCurrency(value.value)}>{data.costs.map((cost) =>
          <option key={cost.currency} value={cost.currency}>{cost.currency}</option>)}</Select></Field>}
    </div>
    <Button onClick={() => applyQuery({ ...query, model: undefined, product: undefined, application: undefined, dimensionFilters: {} })}>
      Clear attribution filters</Button>
    <section className="surface" aria-label="Report progress and history">
      <div className="section-heading"><h2>FinOps report</h2><Button size="small" onClick={recent.refresh}>Refresh report history</Button></div>
      <p className="muted">Input pages are checkpointed until exhausted. Maintained analytics reads stored events and buckets; source mode scans gateway records. Input completion is not proof that upstream ingestion is complete.</p>
      <Field label="Resume or reopen a recent report"><Select value={selectedReport ?? ""} onChange={(_, value) => {
        const run = recent.value?.find((item) => item.id === value.value);
        if (run) { pinnedVersion.current = run.readModelVersion ?? undefined; setQuery(run.query); setIncludeCustom(Boolean(run.customDimensions?.length)); setInputKind(run.inputKind ?? "source-scan"); setSelectedReport(run.id); setActive(run); }
      }}><option value="">Current / new report</option>
        {recent.value?.map((run) => <option key={run.id} value={run.id}>
          {run.query.window.start.slice(0, 10)} to {run.query.window.end.slice(0, 10)} | {run.query.groupBy} | {run.status}
        </option>)}
      </Select></Field>
      {recent.error && <Notice error>Report history: {recent.error}</Notice>}
      {active && <div className="report-progress" role="status">
        <p>{active.status === "succeeded" ? (active.inputKind === "read-model" ? "Analytics input completed" : "Source scan completed") : `Report ${active.status}`}: {count(active.scannedRecords)} {active.inputKind === "read-model" ? "input rows read" : "records scanned"}, {count(active.matchedRequests)} matching requests, {count(active.rejectedRecords)} rejected records.</p>
        <p className="muted">Prices captured {new Date(active.pricingSnapshotAt).toLocaleString()}. Report retained until {new Date(active.expiresAt).toLocaleString()}.</p>
        {active.readModelVersion && <p className="muted">Immutable analytics version {active.readModelVersion.slice(0, 12)}. Scan completion is not an ingestion watermark.</p>}
        {active.readModelVersion && <p className="muted">Filters and drill-through stay on this version. Refresh data explicitly selects a newer generation.</p>}
        <div className="inline">
          {(active.status === "pending" || active.status === "running") && <Button onClick={cancel}>Cancel report</Button>}
          {report.error && (active.status === "pending" || active.status === "running") &&
            <Button appearance="primary" onClick={() => { setSelectedReport(active.id); report.refresh(); }}>Resume this report</Button>}
        </div>
      </div>}
    </section>
    {exportError && <Notice error>{exportError}</Notice>}
    <LoadState {...report} retry={refresh} />
    {data && !report.loading && <div>
      <div className="stats-grid">
        <Stat label="Estimated model cost" value={selectedCost ? money(selectedCost.amount, selectedCost.currency) : "Unpriced"}
          caption={selectedCost ? `${selectedCost.currency} | priced usage only, not an invoice` : "Reviewed, effective-dated prices are required"} />
        <Stat label="Observed requests" value={count(data.requests)} caption={`${count(data.scannedRecords)} ${data.inputKind === "read-model" ? "analytics rows read (events/buckets)" : "source records scanned"}`} />
        <Stat label="Prompt + completion tokens" value={count(data.usage.prompt + data.usage.completion)} caption="Subtypes are not added again" />
        <Stat label="Pricing coverage" value={`${Number(data.pricedUsagePercent).toFixed(1)}%`} caption={`${count(data.unpricedRequests)} requests remain unpriced`} />
      </div>
      {data.unpricedRequests > 0 && <Notice>Unpriced usage is included in token counts, but excluded from estimated cost. Missing prices never mean zero cost.</Notice>}
      {data.truncated && <Notice error>The scan limit was reached. These figures are partial and must not be used as complete chargeback totals.</Notice>}
      {data.analysis ? <FinOpsViews data={data} currency={selectedCurrency} customLabels={portal.customDimensionLabels} drill={drill}
        selectedView={view} onViewChange={setView} /> : <>
      <Notice>This report predates six-view analytics. Start a new report for comparable analytical views.</Notice>
      <section className="surface" style={{ marginTop: 24 }}><div className="section-heading"><div>
        <h2>Estimated cost trend</h2><p className="muted">Priced model usage only{selectedCurrency && `, ${selectedCurrency}`}. No invoice, forecast or exchange-rate assumptions.</p>
      </div><span className="tag">Usage estimate</span></div>
        <Trend values={costTrend} label="Estimated model cost" color="#497cbe" format={(value) => selectedCurrency ? money(value, selectedCurrency) : String(value)} />
      </section>
      <div className="dashboard-columns"><section className="surface"><div className="section-heading">
        <div><h2>Token consumption</h2><p className="muted">Daily prompt + completion tokens, UTC</p></div><span className="tag">Observed usage</span></div>
        <Trend values={trend} label="Tokens" />
      </section><section className="surface"><h2>Token composition</h2><div className="composition">
        <div><span>Prompt</span><strong>{count(data.usage.prompt)}</strong></div>
        <div><span>Completion</span><strong>{count(data.usage.completion)}</strong></div>
        <div><span>Cached prompt (subset)</span><strong>{count(data.requests === 0 ||
          data.quality.warningCodes.includes("usage-field-unavailable:promptCachedTokens") ? null : data.usage.cachedPrompt)}</strong></div>
        <div><span>Source total</span><strong>{count(data.usage.total)}</strong></div>
      </div><p className="muted">Cached and other token subtypes can overlap. An unavailable source total is not inferred.</p></section></div>
      <Breakdown data={data} groupBy={query.groupBy} currency={selectedCurrency} />
      </>}
      <QualityNote quality={data.quality} warnings={data.warnings} />
    </div>}
  </>;
}

function Breakdown({ data, groupBy, currency }: { data: FinOps; groupBy: Dimension; currency?: string }) {
  const [sort, setSort] = useState<"tokens" | "cost">("tokens");
  const rows = useMemo(() => {
    const groups = new Map<string, { name: string; prompt: number; completion: number; cost: number; priced: boolean }>();
    for (const item of data.aggregates) {
      const name = item.dimensions[groupBy] ?? "Unattributed";
      const row = groups.get(name) ?? { name, prompt: 0, completion: 0, cost: 0, priced: false };
      row.prompt += item.usage.prompt; row.completion += item.usage.completion;
      const cost = item.costs.find((entry) => entry.currency === currency);
      if (cost) { row.cost += Number(cost.amount); row.priced = true; }
      groups.set(name, row);
    }
    return [...groups.values()].sort((a, b) =>
      sort === "tokens" ? b.prompt + b.completion - a.prompt - a.completion : b.cost - a.cost);
  }, [data, groupBy, currency, sort]);
  return <section className="surface"><div className="section-heading"><h2>Allocation by {groupBy}</h2>
    <Field label="Sort allocation"><Select value={sort} onChange={(_, value) => setSort(value.value === "cost" ? "cost" : "tokens")}>
      <option value="tokens">Most tokens</option><option value="cost">Highest estimated cost</option></Select></Field></div>
    <div className="table-scroll"><table><caption className="sr-only">Usage and estimated priced cost by {groupBy}</caption>
      <thead><tr><th scope="col">{groupBy}</th><th scope="col">Prompt tokens</th><th scope="col">Completion tokens</th><th scope="col">Priced cost{currency && ` (${currency})`}</th></tr></thead>
      <tbody>{rows.map((row) => <tr key={row.name}><th scope="row">{row.name}</th><td>{count(row.prompt)}</td><td>{count(row.completion)}</td>
        <td>{row.priced && currency ? money(row.cost, currency) : "Unpriced"}</td></tr>)}</tbody>
    </table></div>{!rows.length && <p className="muted">No usage observations match the selected window and filters.</p>}
  </section>;
}

export function OperationsPage({ api, portal, onInvestigate }: {
  api: PortalApi; portal?: Portal; onInvestigate?: (prompt: string) => void;
}) {
  const [query, setQuery] = useState<OperationsQuery>(() => initialQuery());
  const [filters, setFilters] = useState<Partial<Record<GatewayDimension, string>>>({});
  const report = useLoad((signal) => api.operations(query, signal), [api, JSON.stringify(query)]);
  const data = report.value;
  function metric(name: string) {
    const item = data?.metrics.find((value) => value.name === name);
    if (!item || item.value === null) return "Unavailable";
    if (item.unit === "percent") return `${Number(item.value).toFixed(2)}%`;
    if (item.unit === "ms") return `${Number(item.value).toFixed(0)} ms`;
    return count(Number(item.value));
  }
  return <>
    <div className="page-toolbar"><TimeFilter apply={next => setQuery(current => ({ ...current, window: next.window }))} /><Button onClick={report.refresh} disabled={report.loading}>Refresh telemetry</Button></div>
    {portal && <section className="surface"><h2>Gateway log filters</h2><p className="muted">Use APIM identifiers here. Model names and application labels from the usage store are separate attribution signals.</p>
      <div className="filter-bar">{(["backend", "product_id", "subscription_id", "region", "api_id", "operation_name"] as const).map(name =>
        <Field label={`Gateway ${name.replaceAll("_", " ")} filter`} key={name}><Input value={filters[name] ?? ""}
          onChange={(_, data) => setFilters(current => ({ ...current, [name]: data.value }))} /></Field>)}</div>
      <Button onClick={() => setQuery(current => ({ ...current, gatewayFilters: Object.fromEntries(
        Object.entries(filters).filter(([, value]) => value?.trim()).map(([name, value]) => [name, value?.trim()])
      ) }))}>Apply gateway filters</Button>
    </section>}
    <Notice>Operational signals come from APIM gateway logs. No traffic does not prove gateway availability, and HTTP 4xx responses are not server errors.</Notice>
    <LoadState {...report} retry={report.refresh} />
    {data && !report.loading && <>
      <div className="stats-grid">
        <Stat label="Gateway requests" value={metric("requests")} caption="Observed gateway log entries" />
        <Stat label="Non-server-error rate" value={metric("non-server-error-rate")} caption="Share without HTTP 5xx; includes HTTP 4xx" />
        <Stat label="P95 gateway latency" value={metric("latency-p95")} caption="Global percentile, not average of percentiles" />
        <Stat label="Throttled requests" value={metric("throttled")} caption="HTTP 429 responses" />
      </div>
      <div className="dashboard-columns">
        <section className="surface"><h2>Request volume</h2><Trend label="Gateway requests" values={data.timeline.map((p) => ({ label: p.timestamp, value: p.requests }))} /></section>
        <section className="surface"><h2>Server errors</h2><Trend label="HTTP 5xx" color="#c34c44" values={data.timeline.map((p) => ({ label: p.timestamp, value: p.failures }))} /></section>
      </div>
      <section className="surface"><div className="section-heading"><h2>Investigate next</h2><a href="/assistant">Open gateway assistant</a></div>
        <div className="investigation-grid"><div><h3>Server errors: {metric("server-errors")}</h3><p>Check backend availability and circuit-breaker configuration. Ask Citadel to explain a failed request using its correlation ID.</p></div>
          <div><h3>Authentication failures: {metric("auth-failures")}</h3><p>Review access-contract scope, subscription state and credential references. Never paste a key into chat.</p></div>
          <div><h3>Throttling: {metric("throttled")}</h3><p>Check contract token limits and quotas before preparing a reviewed change. Throttling is often expected governance, not an outage.</p></div></div>
      </section>
      <QualityNote quality={data.quality} warnings={data.warnings} />
    </>}
    {portal && <OperationsInvestigation api={api} portal={portal} window={query.window} onInvestigate={onInvestigate} />}
  </>;
}
