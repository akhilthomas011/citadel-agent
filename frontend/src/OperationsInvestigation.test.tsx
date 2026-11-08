import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { OperationsInvestigation } from "./OperationsInvestigation";
import { Assistant } from "./Assistant";
import { PortalApi } from "./api";
import type { Portal } from "./types";

const window = { start: "2026-11-01T00:00:00Z", end: "2026-11-02T00:00:00Z" };
const portal: Portal = {
  scope: { tenantId: "tenant", hubId: "hub", environment: "test", subscriptionId: "subscription" },
  displayName: "Gateway", allowedActions: ["operations.read", "raw-logs.read", "assistant.use"],
  changeMode: "pull-request",
};
const quality = { source: "log-analytics", state: "unknown", observedAt: window.end, watermark: null, warningCodes: [] };
const cid = "11111111-2222-4333-8444-555555555555";

function routes(values: Record<string, unknown>) {
  const fetch = vi.fn(async (path: string) => {
    if (!(path in values)) throw new Error("Unexpected route: " + path);
    return new Response(JSON.stringify(values[path]), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("operational investigation", () => {
  it("does not fetch request evidence without the additional data grant", async () => {
    const fetch = routes({});
    render(<OperationsInvestigation api={new PortalApi("csrf")} portal={{ ...portal, allowedActions: ["operations.read"] }} window={window} />);
    expect(await screen.findByText(/requires an additional raw-logs grant/)).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("rejects invalid IDs before querying and offers a reviewed, unsent assistant prompt", async () => {
    const fetch = routes({ "/api/operations/evidence": {
      quality, warnings: [], truncated: false, records: [{
        timestamp: window.start, correlationId: cid, responseCode: 502, backendResponseCode: 503,
        totalMs: "123", backendMs: "120", backend: "b1", region: "eastus",
      }],
    } });
    const draft = vi.fn();
    render(<OperationsInvestigation api={new PortalApi("csrf")} portal={portal} window={window} onInvestigate={draft} />);
    fireEvent.change(screen.getByLabelText("APIM correlation/request ID"), { target: { value: "invalid" } });
    fireEvent.click(screen.getByRole("button", { name: "Find request evidence" }));
    expect(await screen.findByText(/Enter the APIM correlation/)).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("APIM correlation/request ID"), { target: { value: cid } });
    fireEvent.click(screen.getByRole("button", { name: "Find request evidence" }));
    fireEvent.click(await screen.findByRole("button", { name: "Review investigation in assistant" }));
    expect(draft).toHaveBeenCalledWith(expect.stringContaining(cid));
    expect(draft.mock.calls[0][0]).toContain("Do not submit a PR");
  });

  it("keeps denied component telemetry and empty alerts distinct from health", async () => {
    routes({
      "/api/operations/pipeline": {
        quality, warnings: ["component-metrics-are-not-an-ingestion-watermark"],
        components: [{ component: "event_hub", state: "unavailable", observedAt: null, metrics: {},
          warnings: ["component-permission-denied"], portalUrl: null }],
      },
      "/api/operations/alerts": { quality, warnings: ["no-fired-alerts-in-window-not-a-health-probe"], alerts: [], truncated: false },
    });
    render(<OperationsInvestigation api={new PortalApi("csrf")} portal={portal} window={window} />);
    fireEvent.click(screen.getByRole("tab", { name: "Usage pipeline" }));
    expect(await screen.findByText("component permission denied")).toBeInTheDocument();
    expect(screen.getByText("unavailable")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Alerts and incidents" }));
    expect(await screen.findByText(/No fired alert instances were returned/)).toBeInTheDocument();
    expect(screen.getByText(/Empty results do not prove availability/)).toBeInTheDocument();
  });

  it("queues, polls and cancels assistant work without enabling another submission", async () => {
    const conversation = { id: "a".repeat(64), title: "New", status: "idle", messages: [], updatedAt: window.end };
    const fetch = vi.fn(async (path: string, init?: RequestInit) => {
      let value: unknown;
      if (path === "/api/conversations" && init?.method === "POST") value = conversation;
      else if (path === "/api/conversations") value = [];
      else if (path.endsWith("/turns")) value = { ...conversation, status: "queued" };
      else if (path.endsWith("/cancel")) value = { ...conversation, status: "failed", errorCode: "turn-cancelled" };
      else throw new Error("Unexpected route " + path);
      return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetch);
    render(<Assistant api={new PortalApi("csrf")} portal={{ ...portal, allowedActions: ["assistant.use"] }} subjectId="user" />);
    fireEvent.change(screen.getByLabelText("Message Citadel"), { target: { value: "Inspect" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText("Turn durably queued");
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel assistant turn" }));
    await waitFor(() => expect(screen.getByText(/Turn stopped: turn cancelled/)).toBeInTheDocument());
  });
});
