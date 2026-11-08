import { useEffect, useState } from "react";
import { Button, MessageBar, MessageBarBody, Spinner } from "@fluentui/react-components";
import type { ReactNode } from "react";
import type { Quality, State } from "./types";

export const count = (value: number | null) =>
  value === null ? "Unavailable" : new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value);
export const money = (value: number | string, currency: string) =>
  new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 4 }).format(Number(value));
export const readable = (code: string) => code.replaceAll("-", " ").replaceAll(":", ": ");

export function useLoad<T>(load: (signal: AbortSignal) => Promise<T>, dependencies: readonly unknown[]) {
  const [value, setValue] = useState<T>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    load(controller.signal).then(
      (result) => { if (!controller.signal.aborted) setValue(result); },
      (reason: unknown) => {
        if (!controller.signal.aborted) {
          setValue(undefined);
          setError(reason instanceof Error ? reason.message : "The request could not be completed.");
        }
      },
    ).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
    // The caller explicitly supplies the complete query key, like a data-fetching library.
  }, [...dependencies, revision]);
  return { value, error, loading, refresh: () => setRevision((r) => r + 1) };
}

export function Notice({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return <MessageBar intent={error ? "error" : "info"}><MessageBarBody>{children}</MessageBarBody></MessageBar>;
}

export function LoadState({ loading, error, retry }: { loading: boolean; error?: string; retry: () => void }) {
  if (error) return <div className="load-state"><Notice error>{error}</Notice><Button onClick={retry}>Retry</Button>
    <a href="/auth/login">Sign in again</a></div>;
  return loading ? <div className="load-state" role="status"><Spinner size="small" label="Loading live data" /></div> : null;
}

export function StateBadge({ state }: { state: State }) {
  return <span className={`state-badge state-${state}`}><span className="state-dot" />{readable(state)}</span>;
}

export function QualityNote({ quality, warnings }: { quality: Quality; warnings: string[] }) {
  const codes = [...new Set([...quality.warningCodes, ...warnings])];
  return <section className="quality-panel" aria-label="Data quality and provenance">
    <div className="section-heading"><h3>Data confidence</h3><StateBadge state={quality.state} /></div>
    <p>Source: {readable(quality.source)}. Observed {new Date(quality.observedAt).toLocaleString()}.</p>
    <p>Complete-through watermark: {quality.watermark ? new Date(quality.watermark).toLocaleString() : "Not supplied by source"}</p>
    {codes.length > 0 && <details><summary>{codes.length} data notes and limitations</summary>
      <ul>{codes.map((code) => <li key={code}>{readable(code)}</li>)}</ul></details>}
  </section>;
}

export function Stat({ label, value, caption }: { label: string; value: ReactNode; caption?: string }) {
  return <div className="stat-card"><span className="stat-label">{label}</span>
    <strong className="stat-value">{value}</strong>{caption && <span className="stat-caption">{caption}</span>}</div>;
}

export function Trend({ values, label, color = "#147d92", format = count }: {
  values: { label: string; value: number }[]; label: string; color?: string;
  format?: (value: number) => string;
}) {
  if (!values.length) return <div className="empty-chart">No observations in this window. Check data confidence below.</div>;
  const max = Math.max(...values.map((point) => point.value), 1);
  const width = 760, height = 180, pad = 12;
  const points = values.map((point, index) =>
    `${pad + index / Math.max(values.length - 1, 1) * (width - 2 * pad)},${height - pad - point.value / max * (height - pad * 2)}`,
  ).join(" ");
  return <div className="chart">
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
      <title>{label}</title>
      {[0.25, 0.5, 0.75, 1].map((fraction) =>
        <line key={fraction} x1={pad} x2={width - pad} y1={pad + fraction * (height - 2 * pad)}
          y2={pad + fraction * (height - 2 * pad)} stroke="#e4eaf0" strokeDasharray="4 4" />)}
      <polyline fill="none" stroke={color} strokeWidth="3" points={points} />
      {values.length === 1 && <circle cx={pad} cy={height - pad - values[0].value / max * (height - pad * 2)} r="4" fill={color} />}
    </svg>
    <div className="chart-axis"><span>{values[0].label}</span><span>Peak {format(Math.max(...values.map((point) => point.value)))}</span><span>{values.at(-1)?.label}</span></div>
    <details><summary>View chart data</summary><table><thead><tr><th>Time (UTC)</th><th>{label}</th></tr></thead>
      <tbody>{values.map((point) => <tr key={point.label}><td>{point.label}</td><td>{format(point.value)}</td></tr>)}</tbody></table></details>
  </div>;
}
