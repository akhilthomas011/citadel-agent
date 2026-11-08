import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { FINOPS_VIEWS, FinOpsViews } from "./FinOpsViews";
import type { AnalyticalRow, FinOps, FinOpsAnalysis, Usage } from "./types";

const window = { start: "2026-11-03T23:00:00Z", end: "2026-11-04T01:00:00Z" };
const usage = (prompt: number, completion: number, total: number | null): Usage => ({
  prompt, completion, total, cachedPrompt: 5, promptAudio: 0, completionAudio: 0,
  completionReasoning: 0, completionAcceptedPrediction: 1, completionRejectedPrediction: 0,
  promptImage: 0, cachedPromptImage: 0,
});
const row = (dimensions: AnalyticalRow["dimensions"], quantities: Usage, requests: number, currency?: string,
  amount = "0", extra: Partial<AnalyticalRow> = {}): AnalyticalRow => ({
  window, dimensions, requests, usage: quantities, unpricedRequests: currency ? 0 : requests,
  costs: currency ? [{ amount, currency, basis: "usage-estimate" }] : [],
  unavailableTokens: [], pricedUsagePercent: currency ? "100" : "0", ...extra,
});
const alpha = row({ model: "alpha" }, usage(110, 55, 165), 2, "USD", "0.22");
const beta = row({ model: "beta" }, usage(20, 0, 20), 1, "EUR", "0.02");
const missing = row({ model: "unpriced" }, usage(1, 2, 3), 1);
const hourly = [
  { ...alpha, dimensions: {}, window: { start: window.start, end: "2026-11-04T00:00:00Z" } },
  row({}, usage(21, 2, 23), 2, "EUR", "0.02", {
    window: { start: "2026-11-04T00:00:00Z", end: window.end }, unpricedRequests: 1,
  }),
];

export const analyticalFixture: FinOps & { analysis: FinOpsAnalysis } = {
  reportId: "e".repeat(64), sourceScanComplete: true, inputKind: "read-model", readModelVersion: "f".repeat(64),
  requests: 4, usage: usage(131, 57, 188), unpricedRequests: 1,
  costs: [{ amount: "0.22", currency: "USD", basis: "usage-estimate" }, { amount: "0.02", currency: "EUR", basis: "usage-estimate" }],
  pricedUsagePercent: "98.4", rejectedRecords: 0, scannedRecords: 4, truncated: false,
  quality: { source: "cosmos-usage", state: "unknown", observedAt: window.end, watermark: null, warningCodes: [] },
  warnings: [], aggregates: [], models: ["alpha", "beta"], products: ["p1", "p2"], applications: ["app1", "app2"],
  analysis: {
    customDimensions: [], hourOfDay: hourly.map((value, index) => ({ ...value, window, hourUtc: index ? 0 : 23 })),
    profiles: {
      model: [alpha, beta, missing],
      deployment: [alpha, beta, missing].map((value, index) => ({ ...value, dimensions: { deployment: ["da", "db", "dx"][index] } })),
      product: [
        row({ product: "p1" }, usage(111, 57, 168), 3, "USD", "0.22", { unpricedRequests: 1 }),
        row({ product: "p2" }, usage(20, 0, 20), 1, "EUR", "0.02"),
      ],
      product_deployment: [
        { ...alpha, dimensions: { product: "p1", deployment: "da" } },
        { ...beta, dimensions: { product: "p2", deployment: "db" } },
        { ...missing, dimensions: { product: "p1", deployment: "dx" } },
      ],
      backend: [
        { ...alpha, dimensions: { backend: "b1" } }, { ...beta, dimensions: { backend: "b2" } },
        { ...missing, dimensions: { backend: null } },
      ],
      model_backend: [
        { ...alpha, dimensions: { model: "alpha", backend: "b1" } },
        { ...beta, dimensions: { model: "beta", backend: "b2" } },
        { ...missing, dimensions: { model: "unpriced", backend: null } },
      ],
      application: [{ ...alpha, dimensions: { application: "app1" } }, { ...beta, dimensions: { application: "app2" } }],
      region: [{ ...alpha, dimensions: { region: "eastus" } }, { ...beta, dimensions: { region: "westus" } }],
      gateway_application: [{ ...alpha, dimensions: { gateway: "test", application: "app1" } }],
      region_gateway: [{ ...alpha, dimensions: { region: "eastus", gateway: "test" } }],
      daily: hourly, hourly, deployment_daily: [{ ...alpha, dimensions: { deployment: "da" } }],
    },
  },
};

describe("six-view FinOps analytics", () => {
  it("renders all six reference tabs with real tables and charts, not placeholders", async () => {
    render(<FinOpsViews data={analyticalFixture} currency="USD" drill={vi.fn()} />);
    expect(screen.getAllByRole("tab")).toHaveLength(6);
    for (const view of FINOPS_VIEWS) {
      fireEvent.click(screen.getByRole("tab", { name: view.title }));
      await waitFor(() => expect(screen.getByRole("tab", { name: view.title })).toHaveAttribute("aria-selected", "true"));
      expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
      expect(screen.getByRole("tabpanel").querySelector("table")).not.toBeNull();
    }
    expect(screen.getByRole("table", { name: /Date \/ UTC hour matrix/ })).toBeInTheDocument();
  });

  it("keeps currency coordinates separate and unpriced backends visible", () => {
    const { rerender } = render(<FinOpsViews data={analyticalFixture} currency="USD" drill={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Model and Deployment Performance" }));
    const usdScatter = screen.getByRole("img", { name: "Backend priced cost versus observed tokens" });
    expect(usdScatter.querySelectorAll("circle")).toHaveLength(1);
    expect(usdScatter).toHaveTextContent("b1");
    expect(screen.getAllByText("Unpriced").length).toBeGreaterThan(0);
    rerender(<FinOpsViews data={analyticalFixture} currency="EUR" drill={vi.fn()} />);
    expect(screen.getByRole("img", { name: "Backend priced cost versus observed tokens" })).toHaveTextContent("b2");
    expect(screen.queryByText("$0.24")).not.toBeInTheDocument();
  });

  it("drills through attribution, including missing dimensions, and exact UTC hour windows", () => {
    const drill = vi.fn();
    render(<FinOpsViews data={analyticalFixture} currency="USD" drill={drill} />);
    fireEvent.click(screen.getByRole("tab", { name: "Model and Deployment Performance" }));
    fireEvent.click(screen.getAllByRole("button", { name: "Filter Backend: Unattributed" })[0]);
    expect(drill).toHaveBeenCalledWith({ backend: null });
    fireEvent.click(screen.getByRole("tab", { name: "Heatmap" }));
    fireEvent.click(screen.getByRole("button", { name: /^Filter p1 \/ da:/ }));
    expect(drill).toHaveBeenCalledWith({ product: "p1", deployment: "da" });
    fireEvent.click(screen.getByRole("tab", { name: "Peak Usage and Capacity Planning" }));
    fireEvent.click(screen.getByRole("button", { name: "Investigate 2026-11-03 23:00 UTC" }));
    expect(drill).toHaveBeenCalledWith({}, hourly[0].window);
  });

  it("does not infer source totals or missing token subtypes", () => {
    const missingData = { ...analyticalFixture,
      usage: { ...analyticalFixture.usage, total: null },
      quality: { ...analyticalFixture.quality, warningCodes: ["usage-field-unavailable:completionReasoningTokens"] },
    };
    render(<FinOpsViews data={missingData} currency="USD" drill={vi.fn()} />);
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText("Reasoning (subset)").parentElement).toHaveTextContent("Unavailable");
    fireEvent.click(screen.getByRole("tab", { name: "Peak Usage and Capacity Planning" }));
    expect(screen.getByText(/No capacity, quota, forecast or availability is inferred/)).toBeInTheDocument();
  });

  it("offers hourly resolution and sorts exact analytical rows without another source scan", () => {
    render(<FinOpsViews data={analyticalFixture} currency="USD" drill={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Usage and Cost Overview" }));
    fireEvent.change(screen.getByLabelText("Timeline resolution"), { target: { value: "hourly" } });
    expect(screen.getByRole("heading", { name: "hourly token timeline" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Sort hourly usage and cost values"), { target: { value: "time" } });
    expect(screen.getByRole("table", { name: /hourly usage and cost values:/ }).querySelectorAll("tbody tr")).toHaveLength(2);
  });

  it("does not expose unconfigured custom identifiers in application views", () => {
    render(<FinOpsViews data={analyticalFixture} currency="USD" drill={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Application and Gateway Usage" }));
    expect(screen.queryByText(/Custom dimension/)).not.toBeInTheDocument();
  });

  it("does not connect a timeline across an unobserved date gap", () => {
    const data = { ...analyticalFixture, analysis: { ...analyticalFixture.analysis,
      profiles: { ...analyticalFixture.analysis.profiles, daily: [
        hourly[0], { ...hourly[1], window: { start: "2026-11-05T00:00:00Z", end: "2026-11-06T00:00:00Z" } },
      ] },
    } };
    render(<FinOpsViews data={data} currency="USD" drill={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Usage and Cost Overview" }));
    expect(screen.getByRole("img", { name: "daily token timeline" }).querySelectorAll("line")).toHaveLength(1);
  });
});
