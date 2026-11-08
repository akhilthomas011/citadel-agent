import type { Change, Conversation, FinOps, FinOpsReport, Inventory, Operations, Portal, Query, Session } from "./types";
import type { AlertInstances, OperationalAttribution, OperationsQuery, PipelineHealth, RequestEvidenceResult } from "./types";

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
  }
}

export class PortalApi {
  constructor(private readonly csrfToken?: string) {}

  async request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      cache: "no-store",
      signal,
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(this.csrfToken ? { "X-CSRF-Token": this.csrfToken } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      let message = `Request failed (${response.status}).`;
      if (response.status === 401) message = "Your session expired. Sign in again to continue.";
      else {
        try {
          const error: unknown = await response.json();
          if (typeof error === "object" && error && "detail" in error && typeof error.detail === "string")
            message = error.detail;
        } catch {
          message = `Service returned an unreadable error (${response.status}).`;
        }
      }
      throw new ApiError(response.status, message);
    }
    if (response.status === 204) return undefined as T;
    return response.json();
  }

  session = (signal?: AbortSignal) => this.request<Session>("/auth/session", undefined, signal);
  portal = (signal?: AbortSignal) => this.request<Portal>("/api/portal", undefined, signal);
  inventory = (signal?: AbortSignal) => this.request<Inventory>("/api/inventory", undefined, signal);
  conversations = (signal?: AbortSignal) =>
    this.request<Conversation[]>("/api/conversations", undefined, signal);
  createConversation = () => this.request<Conversation>("/api/conversations", {});
  conversation = (id: string, signal?: AbortSignal) =>
    this.request<Conversation>(`/api/conversations/${id}`, undefined, signal);
  turn = (id: string, message: string, requestId: string) =>
    this.request<Conversation>(`/api/conversations/${id}/turns`, { message, requestId });
  cancelTurn = (id: string) => this.request<Conversation>(`/api/conversations/${id}/cancel`, {});
  changes = (signal?: AbortSignal) => this.request<Change[]>("/api/changes", undefined, signal);
  decision = (change: Change, action: "approve" | "execute", confirmation?: string) =>
    this.request<Change>(`/api/changes/${change.id}/${action}`, {
      approvalHash: change.approvalHash, confirmation,
    });
  recentReports = (signal?: AbortSignal) =>
    this.request<FinOpsReport[]>("/api/finops/reports", undefined, signal);
  getReport = (id: string, signal?: AbortSignal) =>
    this.request<FinOpsReport>(`/api/finops/reports/${id}`, undefined, signal);
  cancelReport = (id: string) =>
    this.request<FinOpsReport>(`/api/finops/reports/${id}/cancel`, {});
  async finops(
    query: Query, signal?: AbortSignal, progress?: (run: FinOpsReport) => void, reportId?: string,
    inputKind?: "source-scan" | "read-model",
    includeCustomDimensions = false,
    readModelVersion?: string,
  ): Promise<FinOps> {
    let run = reportId ? await this.getReport(reportId, signal) :
      await this.request<FinOpsReport>("/api/finops/reports", {
        requestId: crypto.randomUUID(), query, inputKind, includeAnalysis: true, includeCustomDimensions, readModelVersion,
      }, signal);
    for (;;) {
      signal?.throwIfAborted();
      progress?.(run);
      if (run.status === "succeeded") {
        if (!run.result?.sourceScanComplete || run.result.reportId !== run.id)
          throw new ApiError(502, "The service did not return a matching completed report.");
        return run.result;
      }
      if (run.status === "failed")
        throw new ApiError(409, `Report failed (${run.errorCode ?? "unknown"}). Narrow the time window/grouping or contact your administrator.`);
      if (run.status === "cancelled") throw new ApiError(409, "This report was cancelled. Start a new report.");
      await waitForReport(signal);
      run = await this.request<FinOpsReport>(`/api/finops/reports/${run.id}/advance`, {}, signal);
    }
  }
  operations = (query: OperationsQuery, signal?: AbortSignal) =>
    this.request<Operations>("/api/operations/query", query, signal);
  requestEvidence = (window: Query["window"], correlationId: string, signal?: AbortSignal) =>
    this.request<RequestEvidenceResult>("/api/operations/evidence", { window, correlationId }, signal);
  operationAlerts = (query: Query, signal?: AbortSignal) =>
    this.request<AlertInstances>("/api/operations/alerts", query, signal);
  pipelineHealth = (query: Query, signal?: AbortSignal) =>
    this.request<PipelineHealth>("/api/operations/pipeline", query, signal);
  operationalAttribution = (query: Query, signal?: AbortSignal) =>
    this.request<OperationalAttribution>("/api/operations/attribution", query, signal);
  compatibility = (signal?: AbortSignal) => this.request<{
    state: string; upstreamCommit?: string; supportedRelease?: Record<string, string>; knownNoiseCount?: number;
    findings: { level: string; area: string; message: string }[]; warnings: string[];
  }>("/api/operations/compatibility", undefined, signal);
  exportFinops = (reportId: string) =>
    this.request<{ filename: string; content: string }>(`/api/finops/reports/${reportId}/export`, {});
  logout = () => this.request<void>("/auth/logout", {});
}

function waitForReport(signal?: AbortSignal): Promise<void> {
  signal?.throwIfAborted();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", aborted);
      resolve();
    }, 300);
    function aborted() {
      clearTimeout(timer);
      reject(signal?.reason ?? new DOMException("Report request aborted.", "AbortError"));
    }
    signal?.addEventListener("abort", aborted, { once: true });
  });
}
