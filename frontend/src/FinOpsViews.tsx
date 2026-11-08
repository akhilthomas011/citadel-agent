import { useMemo, useState } from "react";
import { Field, Select, Tab, TabList } from "@fluentui/react-components";
import { count, money, Notice, Stat } from "./components";
import type { AnalyticalRow, Dimension, FinOps, Query, Usage } from "./types";

export const FINOPS_VIEWS = [
  { id: "utilization", title: "Utilization" },
  { id: "heatmap", title: "Heatmap" },
  { id: "overview", title: "Usage and Cost Overview" },
  { id: "performance", title: "Model and Deployment Performance" },
  { id: "applications", title: "Application and Gateway Usage" },
  { id: "capacity", title: "Peak Usage and Capacity Planning" },
] as const;
export type FinOpsView = typeof FINOPS_VIEWS[number]["id"];
type Measure = "observed" | "total" | "cost";
type Filters = Partial<Record<Dimension, string | null>>;
type Drill = (filters: Filters, window?: Query["window"]) => void;

export const DIMENSION_LABELS: Record<Dimension, string> = {
  model: "Model", deployment: "Deployment", product: "Product", application: "Application",
  backend: "Backend", region: "Region", gateway: "Gateway", service_type: "Service type",
  operation_name: "Operation", ai_gateway_id: "AI gateway ID",
  custom_dimension_1: "Custom dimension 1", custom_dimension_2: "Custom dimension 2",
};
const TOKEN_FIELDS: { field: keyof Usage; source: string; label: string }[] = [
  { field: "prompt", source: "promptTokens", label: "Prompt" },
  { field: "completion", source: "responseTokens", label: "Completion" },
  { field: "total", source: "totalTokens", label: "Source total" },
  { field: "cachedPrompt", source: "promptCachedTokens", label: "Cached prompt (subset)" },
  { field: "promptAudio", source: "promptAudioTokens", label: "Prompt audio (subset)" },
  { field: "completionAudio", source: "completionAudioTokens", label: "Completion audio (subset)" },
  { field: "completionReasoning", source: "completionReasoningTokens", label: "Reasoning (subset)" },
  { field: "completionAcceptedPrediction", source: "completionAcceptedPredictionTokens", label: "Accepted prediction (subset)" },
  { field: "completionRejectedPrediction", source: "completionRejectedPredictionTokens", label: "Rejected prediction (subset)" },
  { field: "promptImage", source: "promptImageTokens", label: "Prompt image (subset)" },
  { field: "cachedPromptImage", source: "promptCachedImageTokens", label: "Cached image (subset)" },
];

const rowKey = (row: AnalyticalRow) => `${row.window.start}:${row.hourUtc ?? ""}:${JSON.stringify(row.dimensions)}`;
const rowName = (row: AnalyticalRow) => row.hourUtc != null ? `${String(row.hourUtc).padStart(2, "0")}:00 UTC` :
  Object.values(row.dimensions).map(value => value ?? "Unattributed").join(" / ") || `${row.window.start.slice(0, 16)} UTC`;
const rowCost = (row: AnalyticalRow, currency?: string) => row.costs.find(cost => cost.currency === currency);

function measureValue(row: AnalyticalRow, measure: Measure, currency?: string): number | null {
  if (measure === "observed") return row.usage.prompt + row.usage.completion;
  if (measure === "total") return row.usage.total;
  const cost = rowCost(row, currency);
  return cost ? Number(cost.amount) : null;
}

function formatMeasure(row: AnalyticalRow, measure: Measure, currency?: string): string {
  if (measure !== "cost") return count(measureValue(row, measure));
  const cost = rowCost(row, currency);
  if (cost && currency) return money(cost.amount, currency);
  return row.costs.length ? `Other currency: ${row.costs.map(value => value.currency).join(", ")}` : "Unpriced";
}

function sortRows(rows: AnalyticalRow[], measure: Measure, currency?: string) {
  return [...rows].sort((a, b) => {
    const x = measureValue(a, measure, currency), y = measureValue(b, measure, currency);
    if (x === null) return y === null ? rowName(a).localeCompare(rowName(b)) : 1;
    if (y === null) return -1;
    return y - x || rowName(a).localeCompare(rowName(b));
  });
}

interface TableProps {
  title: string;
  rows: AnalyticalRow[];
  currency?: string;
  drill: Drill;
  labels: Record<Dimension, string>;
}

export function AnalysisTable({ title, rows, currency, drill, labels }: TableProps) {
  const [sort, setSort] = useState<"time" | "observed" | "cost" | "requests">("observed");
  const dimensions = [...new Set(rows.flatMap(row => Object.keys(row.dimensions)))]
    .filter((value): value is Dimension => value in DIMENSION_LABELS);
  const ordered = useMemo(() => {
    if (sort === "cost" || sort === "observed") return sortRows(rows, sort, currency);
    return [...rows].sort((a, b) => sort === "requests" ? b.requests - a.requests :
      (a.hourUtc != null && b.hourUtc != null ? a.hourUtc - b.hourUtc : a.window.start.localeCompare(b.window.start)));
  }, [rows, sort, currency]);
  return <section className="surface">
    <div className="section-heading"><h3>{title}</h3><Field label={`Sort ${title}`}><Select value={sort}
      onChange={(_, value) => setSort(value.value === "cost" ? "cost" : value.value === "time" ? "time" : value.value === "requests" ? "requests" : "observed")}>
      <option value="observed">Most observed tokens</option><option value="cost">Highest priced cost</option>
      <option value="requests">Most requests</option><option value="time">UTC order</option>
    </Select></Field></div>
    <div className="table-scroll" tabIndex={0} role="region" aria-label={`${title} scrollable data`}>
      <table><caption>{title}: exact report breakdown, UTC</caption><thead><tr>
        {dimensions.length ? dimensions.map(name => <th scope="col" key={name}>{labels[name]}</th>) :
          <th scope="col">{rows.some(row => row.hourUtc != null) ? "Hour of day (UTC)" : "Window (UTC)"}</th>}
        <th scope="col">Requests</th><th scope="col">Prompt + completion</th><th scope="col">Source total</th>
        <th scope="col">Priced cost{currency && ` (${currency})`}</th><th scope="col">Unpriced requests</th>
        <th scope="col">Pricing coverage</th>
      </tr></thead><tbody>{ordered.map(row => <tr key={rowKey(row)}>
        {dimensions.length ? dimensions.map(name => <th scope="row" key={name}><button className="data-link"
          onClick={() => drill({ [name]: row.dimensions[name] ?? null })}
          aria-label={`Filter ${labels[name]}: ${row.dimensions[name] ?? "Unattributed"}`}>
          {row.dimensions[name] ?? "Unattributed"}</button></th>) :
          <th scope="row">{row.hourUtc != null ? rowName(row) : <button className="data-link"
            onClick={() => drill({}, row.window)} aria-label={`Investigate ${rowName(row)}`}>
            {row.window.start.slice(0, 16)} to {row.window.end.slice(0, 16)}</button>}</th>}
        <td>{count(row.requests)}</td><td>{count(row.usage.prompt + row.usage.completion)}</td>
        <td>{count(row.usage.total)}</td>
        <td title={rowCost(row, currency) ? `${rowCost(row, currency)?.amount} ${currency}` : "No matched price in this currency"}>
          {formatMeasure(row, "cost", currency)}</td><td>{count(row.unpricedRequests)}</td>
        <td>{Number(row.pricedUsagePercent).toFixed(1)}%</td>
      </tr>)}</tbody></table></div>
    {!rows.length && <p className="muted">No observations match this report. Missing data is not a healthy or free service.</p>}
  </section>;
}

function Bars({ title, rows, measure, currency, drill }: {
  title: string; rows: AnalyticalRow[]; measure: Measure; currency?: string; drill: Drill;
}) {
  const shown = sortRows(rows, measure, currency).slice(0, 12);
  const max = Math.max(1, ...shown.map(row => measureValue(row, measure, currency) ?? 0));
  return <section className="surface"><h3>{title}</h3><div className="analytical-bars" aria-label={title}>
    {shown.map(row => <button key={rowKey(row)} className="analytical-bar" onClick={() => drill(row.dimensions)}
      aria-label={`${title}: ${rowName(row)}, ${formatMeasure(row, measure, currency)}; filter this attribution`}>
      <span>{rowName(row)}</span><span className="bar-track"><span
        style={{ width: `${(measureValue(row, measure, currency) ?? 0) / max * 100}%` }} /></span>
      <strong>{formatMeasure(row, measure, currency)}</strong>
    </button>)}</div>
    {!shown.length && <p className="muted">No observed usage.</p>}
    <p className="muted">{rows.length > 12 ? "Largest 12 entries shown; the table and CSV contain every entry." :
      "Select a bar to apply its attribution filter. Exact values remain in the table and CSV."}</p>
  </section>;
}

function Heatmap({ rows, measure, currency, drill }: {
  rows: AnalyticalRow[]; measure: Measure; currency?: string; drill: Drill;
}) {
  const sorted = sortRows(rows, measure, currency);
  const max = Math.max(1, ...sorted.map(row => measureValue(row, measure, currency) ?? 0));
  return <section className="surface"><h3>Product / deployment heatmap</h3>
    <p className="muted">An accessible alternative to the reference treemap. Colour is relative to this report only.</p>
    <div className="analytical-heatmap" aria-label="Product and deployment heatmap">
      {sorted.map(row => <button className="heat-tile" key={rowKey(row)} onClick={() => drill(row.dimensions)}
        style={{ backgroundColor: `rgba(20,125,146,${0.05 + (measureValue(row, measure, currency) ?? 0) / max * 0.18})` }}
        aria-label={`Filter ${rowName(row)}: ${formatMeasure(row, measure, currency)}`}>
        <span>{rowName(row)}</span><strong>{formatMeasure(row, measure, currency)}</strong>
        <small>{count(row.requests)} requests</small>
      </button>)}
    </div>{!sorted.length && <p className="muted">No heatmap observations.</p>}
  </section>;
}

function Timeline({ title, rows, measure, currency }: {
  title: string; rows: AnalyticalRow[]; measure: Measure; currency?: string;
}) {
  const ordered = [...rows].sort((a, b) => (a.hourUtc ?? -1) - (b.hourUtc ?? -1) ||
    a.window.start.localeCompare(b.window.start));
  const max = Math.max(1, ...ordered.map(row => measureValue(row, measure, currency) ?? 0));
  const x = (index: number) => 20 + index / Math.max(ordered.length - 1, 1) * 720;
  const y = (value: number) => 165 - value / max * 145;
  return <section className="surface"><h3>{title}</h3>
    {!ordered.length ? <p className="muted">No observations for this timeline.</p> : <>
      <svg className="analytical-plot" viewBox="0 0 760 185" role="img" aria-label={title}>
        <title>{title}; use the matching data table for exact values</title>
        <line x1="20" y1="165" x2="740" y2="165" stroke="#aebfce" />
        {ordered.map((row, index) => {
          const value = measureValue(row, measure, currency);
          const prior = index ? ordered[index - 1] : undefined;
          const adjacent = prior && (row.hourUtc != null && prior.hourUtc != null ?
            row.hourUtc === prior.hourUtc + 1 : Date.parse(prior.window.end) === Date.parse(row.window.start));
          const previous = prior && adjacent ? measureValue(prior, measure, currency) : null;
          return value === null ? null : <g key={rowKey(row)}>
            {previous !== null && <line x1={x(index - 1)} y1={y(previous)} x2={x(index)} y2={y(value)} stroke="#147d92" strokeWidth="2" />}
            <circle cx={x(index)} cy={y(value)} r="3" fill="#147d92"><title>{rowName(row)}: {formatMeasure(row, measure, currency)}</title></circle>
          </g>;
        })}
      </svg>
      <p className="muted">{rowName(ordered[0])} to {rowName(ordered[ordered.length - 1])}. Unavailable points are gaps, not zero.</p>
    </>}
  </section>;
}

function Scatter({ rows, currency }: { rows: AnalyticalRow[]; currency?: string }) {
  const points = rows.filter(row => rowCost(row, currency) != null);
  const maxX = Math.max(1, ...points.map(row => Number(rowCost(row, currency)?.amount)));
  const maxY = Math.max(1, ...points.map(row => row.usage.prompt + row.usage.completion));
  return <section className="surface"><h3>Backend cost / token scatter</h3>
    <p className="muted">X: priced estimated cost{currency && ` (${currency})`}. Y: observed prompt + completion.
      This is an economics comparison, not a latency or availability SLO.</p>
    {points.length ? <svg className="analytical-plot" viewBox="0 0 760 190" role="img" aria-label="Backend priced cost versus observed tokens">
      <title>Backend cost versus tokens; exact values are in the backend table</title>
      <line x1="20" y1="165" x2="740" y2="165" stroke="#aebfce" /><line x1="20" y1="165" x2="20" y2="15" stroke="#aebfce" />
      {points.map(row => <circle key={rowKey(row)} cx={20 + Number(rowCost(row, currency)?.amount) / maxX * 720}
        cy={165 - (row.usage.prompt + row.usage.completion) / maxY * 145} r="6" fill="#147d92" fillOpacity="0.65">
        <title>{rowName(row)}: {formatMeasure(row, "cost", currency)}, {count(row.usage.prompt + row.usage.completion)} tokens</title>
      </circle>)}
    </svg> : <p className="muted">No priced backend observations for this currency.</p>}
    <p className="muted">Unpriced entries remain in the backend table and are excluded from cost coordinates.</p>
  </section>;
}

function PeakGrid({ rows, measure, currency, drill }: { rows: AnalyticalRow[]; measure: Measure; currency?: string; drill: Drill }) {
  const dates = [...new Set(rows.map(row => row.window.start.slice(0, 10)))].sort();
  const cells = new Map(rows.map(row => [`${row.window.start.slice(0, 10)}:${new Date(row.window.start).getUTCHours()}`, row]));
  return <section className="surface"><h3>Date / hour investigation matrix</h3>
    <div className="table-scroll" tabIndex={0} role="region" aria-label="Date and UTC hour matrix">
      <table><caption>Date / UTC hour matrix: select an observed hour to investigate its exact window</caption>
        <thead><tr><th scope="col">Date (UTC)</th>{Array.from({ length: 24 }, (_, hour) =>
          <th scope="col" key={hour}>{String(hour).padStart(2, "0")}</th>)}</tr></thead>
        <tbody>{dates.map(date => <tr key={date}><th scope="row">{date}</th>{Array.from({ length: 24 }, (_, hour) => {
          const row = cells.get(`${date}:${hour}`);
          return <td key={hour}>{row ? <button className="data-link" onClick={() => drill({}, row.window)}
            aria-label={`Investigate ${date} ${String(hour).padStart(2, "0")}:00 UTC`}>
            {formatMeasure(row, measure, currency)}</button> : <span aria-label="No observation">n/a</span>}</td>;
        })}</tr>)}</tbody>
      </table>
    </div><p className="muted">A blank hour means no recorded observation, not verified zero demand. Boundary hours may be partial.</p>
  </section>;
}

function GatewayApplications({ rows, drill }: { rows: AnalyticalRow[]; drill: Drill }) {
  const gateways = new Map<string | null, Set<string>>();
  for (const row of rows) {
    const gateway = row.dimensions.gateway ?? null;
    const applications = gateways.get(gateway) ?? new Set<string>();
    if (row.dimensions.application != null) applications.add(row.dimensions.application);
    gateways.set(gateway, applications);
  }
  return <section className="surface"><h3>Distinct observed applications by gateway</h3>
    <table><caption>Observed application counts by gateway, excluding absent application labels</caption>
      <thead><tr><th scope="col">Gateway</th><th scope="col">Distinct applications</th></tr></thead>
      <tbody>{[...gateways].map(([gateway, applications]) => <tr key={gateway ?? "unattributed"}>
        <th scope="row"><button className="data-link" onClick={() => drill({ gateway })}>{gateway ?? "Unattributed"}</button></th>
        <td>{count(applications.size)}</td>
      </tr>)}</tbody></table>
  </section>;
}

export function FinOpsViews({ data, currency, customLabels = {}, drill, selectedView, onViewChange }: {
  data: FinOps; currency?: string; customLabels?: Partial<Record<Dimension, string>>; drill: Drill;
  selectedView?: FinOpsView; onViewChange?: (view: FinOpsView) => void;
}) {
  const [localView, setLocalView] = useState<FinOpsView>("utilization");
  const view = selectedView ?? localView;
  const [measure, setMeasure] = useState<Measure>("observed");
  const [grain, setGrain] = useState<"daily" | "hourly">("daily");
  const labels = { ...DIMENSION_LABELS, ...customLabels };
  const analysis = data.analysis;
  if (!analysis) return <Notice>This report predates six-view analytics. Start a new report to build comparable projections.</Notice>;
  const rows = (profile: string) => analysis.profiles[profile] ?? [];
  const table = (profile: string, title: string) => <AnalysisTable key={profile} title={title} rows={rows(profile)}
    currency={currency} labels={labels} drill={drill} />;
  const bars = (profile: string, title: string, metric: Measure = measure) => <Bars title={title} rows={rows(profile)}
    measure={metric} currency={currency} drill={drill} />;
  const knownTotal = data.usage.total === null ? "Unavailable" : count(data.usage.total);
  const peak = sortRows(rows("hourly"), measure, currency)[0];
  const distinct = (profile: Dimension) => rows(profile).filter(row => row.dimensions[profile] != null).length;
  return <div className="finops-analysis">
    <TabList className="finops-tabs" aria-label="FinOps analytical views" selectedValue={view}
      onTabSelect={(_, event) => {
        const selected = FINOPS_VIEWS.find(item => item.id === event.value);
        if (selected) { if (onViewChange) onViewChange(selected.id); else setLocalView(selected.id); }
      }}>
      {FINOPS_VIEWS.map(item => <Tab key={item.id} value={item.id} id={`finops-tab-${item.id}`}
        aria-controls={`finops-panel-${item.id}`}>{item.title}</Tab>)}
    </TabList>
    <div className="filter-bar"><Field label="Analytical measure"><Select value={measure} onChange={(_, event) =>
      setMeasure(event.value === "total" ? "total" : event.value === "cost" ? "cost" : "observed")}>
      <option value="observed">Observed prompt + completion</option><option value="total">Source total tokens</option>
      <option value="cost">Priced estimated cost</option>
    </Select></Field><Stat label="Filtered source total tokens" value={knownTotal} caption="Missing totals are not reconstructed" />
      <p className="muted">All six views use this report's window, filters, prices and version. Estimates are not invoices.</p></div>
    <section role="tabpanel" id={`finops-panel-${view}`} aria-labelledby={`finops-tab-${view}`} tabIndex={0}>
      {view === "utilization" && <>
        <div className="stats-grid"><Stat label="Distinct observed products" value={count(distinct("product"))} />
          <Stat label="Distinct observed deployments" value={count(distinct("deployment"))} />
          <Stat label="Source total tokens" value={knownTotal} caption="Never reconstructed from token subtypes" /></div>
        <section className="surface"><h2>Extended token composition</h2><div className="composition">
          {TOKEN_FIELDS.map(item => <div key={item.field}><span>{item.label}</span><strong>{count(data.requests === 0 ||
            data.quality.warningCodes.includes(`usage-field-unavailable:${item.source}`) ? null : data.usage[item.field] ?? null)}</strong></div>)}
        </div><p className="muted">Cached, audio, image, reasoning and prediction counts can overlap their parent totals. Do not add them again.</p></section>
        <div className="dashboard-columns">{bars("product", "Product consumption")}{bars("deployment", "Deployment consumption")}</div>
        {table("product_deployment", "Product / deployment utilization")}
      </>}
      {view === "heatmap" && <>
        <Heatmap rows={rows("product_deployment")} measure={measure} currency={currency} drill={drill} />
        <div className="dashboard-columns">{bars("product", "Product proportional usage")}{bars("deployment", "Deployment proportional usage")}</div>
        {table("product_deployment", "Heatmap values")}
        <div className="dashboard-columns"><Timeline title="Daily deployment consumption trend" rows={rows("daily")} measure={measure === "cost" ? "observed" : measure} />
          <Timeline title="Daily deployment priced cost trend" rows={rows("daily")} measure="cost" currency={currency} /></div>
        {table("deployment_daily", "Daily deployment tokens and cost")}
      </>}
      {view === "overview" && <>
        <Field label="Timeline resolution"><Select value={grain} onChange={(_, event) => setGrain(event.value === "hourly" ? "hourly" : "daily")}>
          <option value="daily">Daily UTC</option><option value="hourly">Hourly UTC</option></Select></Field>
        <div className="dashboard-columns"><Timeline title={`${grain} token timeline`} rows={rows(grain)} measure={measure === "cost" ? "observed" : measure} />
          <Timeline title={`${grain} priced cost timeline`} rows={rows(grain)} measure="cost" currency={currency} /></div>
        {table(grain, `${grain} usage and cost values`)}
        <Timeline title="Hour-of-day usage distribution" rows={analysis.hourOfDay} measure={measure} currency={currency} />
        <AnalysisTable title="Hour-of-day usage and cost" rows={analysis.hourOfDay} currency={currency} labels={labels} drill={drill} />
      </>}
      {view === "performance" && <>
        <Notice>Model and deployment performance here describes consumption and economics. Request latency and gateway health remain in Operations.</Notice>
        <div className="dashboard-columns">{bars("model", "Model token comparison", measure === "total" ? "total" : "observed")}
          {bars("model", "Model priced cost comparison", "cost")}</div>
        <Scatter rows={rows("backend")} currency={currency} />
        {table("backend", "Backend scatter values")}{table("model_backend", "Model / backend matrix")}
        {table("deployment", "Deployment economics")}
      </>}
      {view === "applications" && <>
        <div className="dashboard-columns">{bars("application", "Application consumption")}{bars("region", "Region consumption")}</div>
        <GatewayApplications rows={rows("gateway_application")} drill={drill} />
        {table("gateway_application", "Gateway / application requests")}{table("region_gateway", "Region / gateway matrix")}
        {table("application", "Application usage")}{table("product", "Product usage")}
        {analysis.customDimensions.map(name => table(name, `${labels[name]} attribution`))}
      </>}
      {view === "capacity" && <>
        <div className="stats-grid"><Stat label="Peak recorded hourly measure" value={peak ? formatMeasure(peak, measure, currency) : "Unavailable"}
          caption={peak ? `${peak.window.start.slice(0, 16)} UTC; observed bucket, not a capacity limit` : "No observed hourly bucket"} />
          <Stat label="Source total tokens" value={knownTotal} /><Stat label="Observed hourly buckets" value={count(rows("hourly").length)} /></div>
        <Notice>No capacity, quota, forecast or availability is inferred from these observations. Use configured limits and source-completeness evidence before sizing workloads.</Notice>
        <Timeline title="Daily demand trend" rows={rows("daily")} measure={measure} currency={currency} />
        <div className="dashboard-columns"><Timeline title="Hour-of-day demand" rows={analysis.hourOfDay} measure={measure === "cost" ? "observed" : measure} />
          <Timeline title="Hour-of-day priced cost" rows={analysis.hourOfDay} measure="cost" currency={currency} /></div>
        <PeakGrid rows={rows("hourly")} measure={measure} currency={currency} drill={drill} />
        <AnalysisTable title="Peak-hour distribution values" rows={analysis.hourOfDay} currency={currency} labels={labels} drill={drill} />
      </>}
    </section>
    <p className="muted">CSV contains independent labelled profiles; do not add profiles together. Currency rows may repeat usage counts.
      Graph coordinates are display approximations; exported server-side money retains Decimal precision.</p>
  </div>;
}
