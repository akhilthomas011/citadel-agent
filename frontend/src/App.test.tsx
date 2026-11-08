import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { PortalApi } from "./api";
import { Assistant } from "./Assistant";
import { FinOpsPage, OperationsPage } from "./Dashboards";
import type { FinOps, FinOpsReport, Operations, Portal } from "./types";

const portal: Portal = {
  scope: { tenantId: "tenant", hubId: "test", environment: "prod", subscriptionId: "subscription" },
  displayName: "Test gateway",
  allowedActions: ["assistant.use", "finops.read", "operations.read", "change.draft", "change.approve"],
  changeMode: "pull-request",
};
const quality = { source: "cosmos-usage", state: "unknown" as const, observedAt: new Date().toISOString(), watermark: null, warningCodes: ["source-watermark-unavailable", "usage-field-unavailable:promptCachedTokens"] };
const finops: FinOps = {
  reportId: "c".repeat(64), sourceScanComplete: true,
  quality, warnings: ["costs-are-usage-estimates-not-invoices"], requests: 2,
  usage: { prompt: 100, completion: 20, cachedPrompt: 0, total: null },
  costs: [], pricedUsagePercent: "0", unpricedRequests: 2, scannedRecords: 2, rejectedRecords: 0,
  truncated: false, aggregates: [], models: ["gpt-test"], products: [], applications: [],
};
function completedReport(data: FinOps = finops): FinOpsReport {
  return {
    id: data.reportId ?? "c".repeat(64), scope: portal.scope,
    query: { window: { start: "2026-01-01T00:00:00Z", end: "2026-01-02T00:00:00Z" }, groupBy: "model" },
    status: "succeeded", scannedRecords: data.scannedRecords, matchedRequests: data.requests,
    rejectedRecords: 0, updatedAt: new Date().toISOString(),
    expiresAt: new Date(Date.now() + 86400000).toISOString(), pricingSnapshotAt: new Date().toISOString(),
    errorCode: null, result: data,
  };
}
function reportRoutes(data: FinOps = finops) {
  return { "GET /api/finops/reports": [], "POST /api/finops/reports": completedReport(data) };
}

beforeEach(() => { window.history.replaceState(null, "", "/"); });

function mockRoutes(routes: Record<string, unknown>) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${url}`;
    const target = key in routes ? key : url;
    if (!(target in routes)) throw new Error(`Unexpected request ${key}`);
    return new Response(JSON.stringify(routes[target]), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("hosted portal", () => {
  it("shows Entra sign-in without fetching gateway data for an anonymous browser", async () => {
    const fetch = mockRoutes({ "/auth/session": { authenticated: false } });
    render(<App />);
    expect(await screen.findByRole("link", { name: "Sign in with Microsoft Entra" })).toHaveAttribute("href", "/auth/login");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("does not query a sensitive page when the server capability list denies it", async () => {
    window.history.replaceState(null, "", "/finops");
    const fetch = mockRoutes({
      "/auth/session": { authenticated: true, csrfToken: "csrf", user: { subjectId: "viewer", roles: [] } },
      "/api/portal": { ...portal, allowedActions: ["assistant.use"] },
    });
    render(<App />);
    expect(await screen.findByText(/This page requires additional application roles/)).toBeInTheDocument();
    expect(fetch.mock.calls.some(([path]) => path.startsWith("/api/finops/"))).toBe(false);
  });

  it("renders unpriced costs and absent source totals as unavailable rather than free", async () => {
    mockRoutes(reportRoutes());
    render(<FinOpsPage api={new PortalApi("csrf")} portal={portal} />);
    expect(await screen.findByText(/Missing prices never mean zero cost/)).toBeInTheDocument();
    expect(screen.getAllByText("Unpriced").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Unavailable")).toHaveLength(2);
    expect(screen.queryByText("$0.0000")).not.toBeInTheDocument();
    expect(screen.getByText("Not supplied by source", { exact: false })).toBeInTheDocument();
  });

  it("blocks a reversed custom time range before querying the server", async () => {
    const fetch = mockRoutes(reportRoutes());
    render(<FinOpsPage api={new PortalApi("csrf")} portal={portal} />);
    await screen.findByText("Estimated model cost");
    fireEvent.change(screen.getByLabelText("Reporting window"), { target: { value: "custom" } });
    fireEvent.change(screen.getByLabelText("From (UTC)"), { target: { value: "2026-01-02T00:00" } });
    fireEvent.change(screen.getByLabelText("Until, exclusive (UTC)"), { target: { value: "2026-01-01T00:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply window" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose an ordered UTC window");
    expect(fetch.mock.calls.filter(([path, init]) => path === "/api/finops/reports" && init?.method === "POST")).toHaveLength(1);
  });

  it("shows source-scan progress and exports only the completed report ID", async () => {
    const finished = completedReport();
    const pending = { ...finished, status: "pending", scannedRecords: 500, matchedRequests: 1, result: null };
    const fetch = mockRoutes({
      "GET /api/finops/reports": [],
      "POST /api/finops/reports": pending,
      [`/api/finops/reports/${finished.id}/advance`]: finished,
      [`/api/finops/reports/${finished.id}/export`]: { filename: "citadel-finops.csv", content: "same-report" },
    });
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:test") });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<FinOpsPage api={new PortalApi("csrf")} portal={{ ...portal, allowedActions: [...portal.allowedActions, "export.create"] }} />);
    expect(await screen.findByText(/Report pending: 500 records scanned/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeDisabled();
    expect(screen.queryByText("Estimated model cost", { exact: true })).not.toBeInTheDocument();
    await screen.findByText(/Source scan completed/);
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(fetch.mock.calls.some(([path]) => path === `/api/finops/reports/${finished.id}/export`)).toBe(true));
    const exported = fetch.mock.calls.find(([path]) => path.endsWith("/export"));
    expect(JSON.parse(String(exported?.[1]?.body))).toEqual({});
    expect(fetch.mock.calls.some(([path]) => path === "/api/finops/export")).toBe(false);
  });

  it("settles cancellation without leaving loading, old totals or export enabled", async () => {
    const finished = completedReport();
    const pending: FinOpsReport = { ...finished, status: "pending", scannedRecords: 0, matchedRequests: 0, result: null };
    const cancelled: FinOpsReport = { ...pending, status: "cancelled" };
    const fetch = mockRoutes({
      "GET /api/finops/reports": [],
      "POST /api/finops/reports": pending,
      [`POST /api/finops/reports/${finished.id}/advance`]: cancelled,
      [`POST /api/finops/reports/${finished.id}/cancel`]: cancelled,
    });
    render(<FinOpsPage api={new PortalApi("csrf")} portal={{ ...portal, allowedActions: [...portal.allowedActions, "export.create"] }} />);
    fireEvent.click(await screen.findByRole("button", { name: "Cancel report" }));
    await screen.findByText(/Report cancelled:/);
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh data" })).toBeEnabled());
    expect(screen.queryByRole("progressbar", { name: "Loading live data" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeDisabled();
    expect(screen.queryByText("Estimated model cost", { exact: true })).not.toBeInTheDocument();
    expect(fetch.mock.calls.some(([path]) => path.endsWith(`/${finished.id}/cancel`))).toBe(true);
  });

  it("uses maintained analytics by default and lets users explicitly select a source scan", async () => {
    const finished = completedReport();
    const fetch = mockRoutes({
      "GET /api/finops/reports": [],
      "POST /api/finops/reports": { ...finished, inputKind: "read-model", readModelVersion: "d".repeat(64),
        result: { ...finops, inputKind: "read-model", readModelVersion: "d".repeat(64) } },
    });
    render(<FinOpsPage api={new PortalApi("csrf")} portal={{ ...portal, analyticsEnabled: true }} />);
    await screen.findByText(/Immutable analytics version/);
    expect(screen.getByText(/Analytics input completed/)).toBeInTheDocument();
    expect(screen.getByText(/analytics rows read \(events\/buckets\)/)).toBeInTheDocument();
    const first = fetch.mock.calls.find(([path, init]) => path === "/api/finops/reports" && init?.method === "POST");
    expect(JSON.parse(String(first?.[1]?.body)).inputKind).toBe("read-model");
    fireEvent.change(screen.getByLabelText("Report data input"), { target: { value: "source-scan" } });
    await waitFor(() => {
      const posts = fetch.mock.calls.filter(([path, init]) => path === "/api/finops/reports" && init?.method === "POST");
      expect(posts).toHaveLength(2);
      expect(JSON.parse(String(posts[1][1]?.body)).inputKind).toBe("source-scan");
    });
  });

  it("resumes a report by ID without starting another source scan", async () => {
    const finished = completedReport();
    const pending = { ...finished, status: "pending", result: null };
    const fetch = mockRoutes({
      [`GET /api/finops/reports/${finished.id}`]: pending,
      [`POST /api/finops/reports/${finished.id}/advance`]: finished,
    });
    const observed: string[] = [];
    const result = await new PortalApi("csrf").finops(finished.query, undefined, (run) => observed.push(run.status), finished.id);
    expect(result.reportId).toBe(finished.id);
    expect(observed).toEqual(["pending", "succeeded"]);
    expect(fetch.mock.calls.some(([path]) => path === "/api/finops/reports")).toBe(false);
  });

  it("pins analytical drills without restarting acceptance, and refresh explicitly unpins", async () => {
      const version = "d".repeat(64);
      const data: FinOps = { ...finops, inputKind: "read-model", readModelVersion: version,
        analysis: { profiles: { product_deployment: [{
          window: completedReport().query.window, dimensions: { product: "p1", deployment: "d1" },
          requests: 2, unpricedRequests: 2, usage: finops.usage, unavailableTokens: ["totalTokens"],
          costs: [], pricedUsagePercent: "0",
        }] }, customDimensions: [], hourOfDay: [] } };
      const finished = { ...completedReport(data), inputKind: "read-model", readModelVersion: version };
      const fetch = mockRoutes({ "GET /api/finops/reports": [], "POST /api/finops/reports": finished });
      render(<FinOpsPage api={new PortalApi("csrf")} portal={{ ...portal, analyticsEnabled: true }} />);
      await screen.findByRole("button", { name: "Filter Product: p1" });
      const posts = () => fetch.mock.calls.filter(([path, init]) => path === "/api/finops/reports" && init?.method === "POST");
      expect(posts()).toHaveLength(1);
      fireEvent.click(screen.getByRole("tab", { name: "Heatmap" }));
      fireEvent.click(screen.getByRole("button", { name: "Filter Product: p1" }));
      await waitFor(() => expect(posts()).toHaveLength(2));
      const drilled = JSON.parse(String(posts()[1][1]?.body));
      expect(drilled.readModelVersion).toBe(version);
      expect(drilled.query.dimensionFilters).toEqual({ product: "p1" });
      await waitFor(() => expect(screen.getByRole("button", { name: "Refresh data" })).toBeEnabled());
      expect(screen.getByRole("tab", { name: "Heatmap" })).toHaveAttribute("aria-selected", "true");
      fireEvent.click(screen.getByRole("button", { name: "Refresh data" }));
      await waitFor(() => expect(posts()).toHaveLength(3));
      expect(JSON.parse(String(posts()[2][1]?.body)).readModelVersion).toBeUndefined();
  });

  it("does not render missing operational telemetry as successful requests", async () => {
    const result: Operations = { quality: { ...quality, state: "unavailable" }, warnings: ["log-analytics-not-configured"], metrics: [], timeline: [] };
    mockRoutes({ "/api/operations/query": result });
    render(<OperationsPage api={new PortalApi("csrf")} />);
    await screen.findByText("Gateway requests");
    expect(screen.getAllByText("Unavailable")).toHaveLength(4);
    expect(screen.queryByText("100.00%")).not.toBeInTheDocument();
  });

  it("sends turns with CSRF and an idempotency ID, and treats model HTML as text", async () => {
    const conversation = { id: "a".repeat(64), title: "New", status: "idle", messages: [], updatedAt: new Date().toISOString() };
    const fetch = mockRoutes({
      "GET /api/conversations": [],
      "POST /api/conversations": conversation,
      [`/api/conversations/${conversation.id}/turns`]: {
        ...conversation, messages: [{ role: "assistant", content: "Safe answer\n\n<script>alert('bad')</script>", timestamp: new Date().toISOString() }],
      },
    });
    render(<Assistant api={new PortalApi("csrf-value")} portal={{ ...portal, allowedActions: ["assistant.use"] }} subjectId="user" />);
    fireEvent.change(screen.getByLabelText("Message Citadel"), { target: { value: "Show models" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText("Safe answer")).toBeInTheDocument();
    expect(document.querySelector(".message-text script")).toBeNull();
    await waitFor(() => expect(fetch.mock.calls.some(([path]) => path.endsWith("/turns"))).toBe(true));
    const turn = fetch.mock.calls.find(([path]) => path.endsWith("/turns"));
    expect(turn).toBeDefined();
    expect(turn?.[1]?.headers).toMatchObject({ "X-CSRF-Token": "csrf-value" });
    const body = JSON.parse(String(turn?.[1]?.body));
    expect(body.message).toBe("Show models");
    expect(body.requestId).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("polls an accepted turn and refreshes change review only after completion", async () => {
    const now = new Date().toISOString();
    const api = new PortalApi("csrf");
    const conversation = { id: "b".repeat(64), title: "New", status: "idle" as const, messages: [], errorCode: null, updatedAt: now };
    vi.spyOn(api, "conversations").mockResolvedValue([]);
    vi.spyOn(api, "changes").mockResolvedValue([]);
    vi.spyOn(api, "createConversation").mockResolvedValue(conversation);
    const messages = [{ role: "user" as const, content: "Prepare a reviewed change", timestamp: now }];
    vi.spyOn(api, "turn").mockResolvedValue({ ...conversation, status: "running", messages });
    vi.spyOn(api, "conversation").mockResolvedValue({
      ...conversation, messages: [...messages, { role: "assistant", content: "Preview is ready", timestamp: now }],
    });
    render(<Assistant api={api} portal={{ ...portal, allowedActions: ["assistant.use", "change.draft"] }} subjectId="user" />);
    fireEvent.change(screen.getByLabelText("Message Citadel"), { target: { value: "Prepare a reviewed change" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText(/A turn is still running/)).toBeInTheDocument();
    expect(await screen.findByText("Preview is ready", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(api.conversation).toHaveBeenCalledWith(conversation.id, expect.any(AbortSignal));
    await waitFor(() => expect(api.changes).toHaveBeenCalledTimes(3));
    expect(screen.queryByText(/A turn is still running/)).not.toBeInTheDocument();
  });
});
