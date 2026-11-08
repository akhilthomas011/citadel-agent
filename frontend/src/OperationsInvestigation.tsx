import { useState } from "react";
import { Button, Field, Input, Select, Tab, TabList } from "@fluentui/react-components";
import { PortalApi } from "./api";
import { count, LoadState, Notice, QualityNote, readable, StateBadge, useLoad } from "./components";
import type { Dimension, Portal, Query, RequestEvidenceResult } from "./types";

type Panel = "requests" | "alerts" | "pipeline" | "attribution" | "compatibility";
const panels: { id: Panel; label: string }[] = [
  { id: "requests", label: "Request investigation" }, { id: "alerts", label: "Alerts and incidents" },
  { id: "pipeline", label: "Usage pipeline" }, { id: "attribution", label: "Usage attribution" },
  { id: "compatibility", label: "Supported baseline" },
];
const attributionDimensions: Dimension[] = ["backend", "model", "deployment", "region", "application"];

export function OperationsInvestigation({ api, portal, window, onInvestigate }: {
  api: PortalApi; portal: Portal; window: Query["window"]; onInvestigate?: (prompt: string) => void;
}) {
  const [panel, setPanel] = useState<Panel>("requests");
  const [correlation, setCorrelation] = useState("");
  const [evidence, setEvidence] = useState<RequestEvidenceResult>();
  const [evidenceWindow, setEvidenceWindow] = useState<Query["window"]>();
  const [evidenceCorrelation, setEvidenceCorrelation] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [attributionFilters, setAttributionFilters] = useState<Query["dimensionFilters"]>({});
  const baseQuery: Query = { window, groupBy: "model" };
  const alerts = useLoad(signal => panel === "alerts" ? api.operationAlerts(baseQuery, signal) : Promise.resolve(undefined),
    [api, panel, JSON.stringify(window)]);
  const pipeline = useLoad(signal => panel === "pipeline" ? api.pipelineHealth(baseQuery, signal) : Promise.resolve(undefined),
    [api, panel, JSON.stringify(window)]);
  const attribution = useLoad(signal => panel === "attribution" ? api.operationalAttribution(
    { ...baseQuery, dimensionFilters: attributionFilters }, signal) : Promise.resolve(undefined),
    [api, panel, JSON.stringify(window), JSON.stringify(attributionFilters)]);
  const compatibility = useLoad(signal => panel === "compatibility" ? api.compatibility(signal) : Promise.resolve(undefined), [api, panel]);

  async function investigate() {
    setError(""); setEvidence(undefined);
    if (!/^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(correlation.trim())) {
      setError("Enter the APIM correlation/request ID as a GUID."); return;
    }
    if (Date.parse(window.end) - Date.parse(window.start) > 7 * 86400000) {
      setError("Select a request investigation window of at most seven days."); return;
    }
    setLoading(true);
    try {
      const requested = correlation.trim();
      setEvidence(await api.requestEvidence(window, requested));
      setEvidenceWindow(window); setEvidenceCorrelation(requested);
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Request evidence is unavailable."); }
    finally { setLoading(false); }
  }
  function draft() {
    onInvestigate?.(`Investigate APIM request ${evidenceCorrelation} for the current authorized gateway between ${evidenceWindow?.start} and ${evidenceWindow?.end}. Read scoped evidence and propose a reviewed change only if justified. Do not submit a PR, rotate keys or deploy anything.`);
  }
  return <section className="operations-investigation">
    <TabList className="finops-tabs" selectedValue={panel} aria-label="Operational investigation"
      onTabSelect={(_, data) => { const selected = panels.find(item => item.id === data.value); if (selected) setPanel(selected.id); }}>
      {panels.map(item => <Tab key={item.id} value={item.id} id={`operations-tab-${item.id}`} aria-controls={`operations-panel-${item.id}`}>{item.label}</Tab>)}
    </TabList>
    <section role="tabpanel" id={`operations-panel-${panel}`} aria-labelledby={`operations-tab-${panel}`} tabIndex={0}>
      {panel === "requests" && <section className="surface"><h2>Scoped request evidence</h2>
        <p className="muted">Exact gateway, UTC window and APIM-generated GUID. Bodies, headers, URLs, IP addresses and trace messages are not returned.</p>
        {!portal.allowedActions.includes("raw-logs.read") ? <Notice>Request evidence requires an additional raw-logs grant and an Operations role.</Notice> : <>
          <div className="inline"><Field label="APIM correlation/request ID"><Input value={correlation} onChange={(_, data) => setCorrelation(data.value)} /></Field>
            <Button appearance="primary" disabled={loading} onClick={investigate}>Find request evidence</Button></div>
          <LoadState loading={loading} error={error || undefined} retry={investigate} />
          {evidence && evidenceCorrelation === correlation.trim() && evidenceWindow?.start === window.start && evidenceWindow?.end === window.end && <>
            <div className="table-scroll" tabIndex={0} role="region" aria-label="Request evidence table">
              <table><caption>Redacted request evidence, UTC</caption><thead><tr>
                <th scope="col">Timestamp</th><th scope="col">Gateway response</th><th scope="col">Backend response</th>
                <th scope="col">Total ms</th><th scope="col">Backend ms</th><th scope="col">Backend</th><th scope="col">Region</th>
              </tr></thead><tbody>{evidence.records.map((item, index) => <tr key={`${item.timestamp}:${index}`}>
                <th scope="row">{item.timestamp}</th><td>{item.responseCode ?? "Unavailable"}</td><td>{item.backendResponseCode ?? "Unavailable"}</td>
                <td>{item.totalMs ?? "Unavailable"}</td><td>{item.backendMs ?? "Unavailable"}</td><td>{item.backend ?? "Unattributed"}</td><td>{item.region ?? "Unattributed"}</td>
              </tr>)}</tbody></table>
            </div>
            {onInvestigate && evidence.records.length > 0 && <Button onClick={draft}>Review investigation in assistant</Button>}
            <QualityNote quality={evidence.quality} warnings={evidence.warnings} />
          </>}
        </>}
      </section>}
      {panel === "alerts" && <section className="surface"><h2>Fired gateway alert instances</h2>
        <p className="muted">Read-only alert state. Empty results do not prove availability; historic queries are limited to the last 30 days.</p>
        <LoadState {...alerts} retry={alerts.refresh} />
        {alerts.value && !alerts.loading && <>
          <div className="table-scroll"><table><caption>Scoped fired alerts</caption><thead><tr>
            <th scope="col">Severity</th><th scope="col">State</th><th scope="col">Started (UTC)</th><th scope="col">Evidence</th>
          </tr></thead><tbody>{alerts.value.alerts.map(item => <tr key={item.id}>
            <th scope="row">{item.severity}</th><td>{item.state}</td><td>{item.startedAt}</td>
            <td><a href={item.portalUrl} target="_blank" rel="noreferrer noopener">Inspect alert in Azure</a></td>
          </tr>)}</tbody></table></div>
          {!alerts.value.alerts.length && <p>No fired alert instances were returned. Check source confidence below.</p>}
          <QualityNote quality={alerts.value.quality} warnings={alerts.value.warnings} />
        </>}
      </section>}
      {panel === "pipeline" && <section className="surface"><h2>Usage ingestion component evidence</h2>
        <Notice>These are namespace/app-level telemetry observations and worker refreshes, not an ingestion completeness watermark or an end-to-end availability probe.</Notice>
        <LoadState {...pipeline} retry={pipeline.refresh} />
        {pipeline.value && !pipeline.loading && <>
          <div className="investigation-grid">{pipeline.value.components.map(component => <article key={component.component}>
            <h3>{readable(component.component.replaceAll("_", "-"))}</h3><StateBadge state={component.state} />
            <p>Telemetry observed: {component.observedAt ?? "Unavailable"}</p>
            <dl>{Object.entries(component.metrics).map(([name, value]) => <div key={name}><dt>{readable(name)}</dt><dd>{value}</dd></div>)}</dl>
            <ul>{component.warnings.map(code => <li key={code}>{readable(code)}</li>)}</ul>
            {component.portalUrl && <a href={component.portalUrl} target="_blank" rel="noreferrer noopener">Inspect configured component</a>}
          </article>)}</div>
          <QualityNote quality={pipeline.value.quality} warnings={pipeline.value.warnings} />
        </>}
      </section>}
      {panel === "attribution" && <section className="surface"><h2>Maintained usage request attribution</h2>
        <Notice>This source identifies usage requests by model, deployment, backend, region and application. It does not join those events to gateway response codes or latency, and does not expose financial or custom identifiers.</Notice>
        <div className="filter-bar">{attributionDimensions.map(dimension => <Field key={dimension} label={`Usage ${dimension} filter`}>
          <Select value={dimension in (attributionFilters ?? {}) ? attributionFilters?.[dimension] === null ? "m:" : `v:${attributionFilters?.[dimension]}` : ""}
            onChange={(_, data) => setAttributionFilters(current => {
              const next = { ...current };
              if (!data.value) delete next[dimension]; else next[dimension] = data.value === "m:" ? null : data.value.slice(2);
              return next;
            })}>
            <option value="">All {dimension}</option><option value="m:">Unattributed</option>
            {(attribution.value?.groups[dimension] ?? []).filter(item => item.value !== null).map(item =>
              <option key={item.value} value={`v:${item.value}`}>{item.value}</option>)}
          </Select></Field>)}</div>
        <LoadState {...attribution} retry={attribution.refresh} />
        {attribution.value && !attribution.loading && <>
          <p>Immutable usage version {attribution.value.readModelVersion.slice(0, 12)}</p>
          {Object.entries(attribution.value.groups).map(([dimension, rows]) => <div className="table-scroll" key={dimension}>
            <table><caption>Observed usage requests by {dimension}</caption><thead><tr><th scope="col">{dimension}</th><th scope="col">Requests</th></tr></thead>
              <tbody>{rows.map(item => <tr key={item.value ?? "unattributed"}><th scope="row">{item.value ?? "Unattributed"}</th><td>{count(item.requests)}</td></tr>)}</tbody>
            </table></div>)}
          <QualityNote quality={attribution.value.quality} warnings={attribution.value.warnings} />
        </>}
      </section>}
      {panel === "compatibility" && <section className="surface"><h2>Supported upstream baseline</h2>
        <LoadState {...compatibility} retry={compatibility.refresh} />
        {compatibility.value && !compatibility.loading && <>
          <p>Check result: {readable(compatibility.value.state)}</p>
          <p>Upstream commit: {compatibility.value.upstreamCommit ?? "Unavailable"}</p>
          <dl>{Object.entries(compatibility.value.supportedRelease ?? {}).map(([name, value]) => <div key={name}><dt>{readable(name)}</dt><dd>{value}</dd></div>)}</dl>
          <p>Explicitly allowlisted known-noise findings: {compatibility.value.knownNoiseCount ?? "Unavailable"}</p>
          <ul>{compatibility.value.findings.map((item, index) => <li key={index}>{item.level}: {item.area}: {item.message}</li>)}</ul>
          <ul>{compatibility.value.warnings.map(value => <li key={value}>{readable(value)}</li>)}</ul>
        </>}
      </section>}
    </section>
  </section>;
}
