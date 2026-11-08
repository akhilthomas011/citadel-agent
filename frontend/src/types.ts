export type State = "healthy" | "degraded" | "unavailable" | "unknown" | "stale" | "partial";
export interface Scope {
  tenantId: string;
  hubId: string;
  environment: string;
  subscriptionId: string;
}
export interface Session {
  authenticated: boolean;
  csrfToken?: string;
  user?: { tenantId: string; subjectId: string; roles: string[] };
  expiresAt?: string;
}
export interface Portal {
  scope: Scope;
  displayName: string;
  allowedActions: string[];
  changeMode: string;
  analyticsEnabled?: boolean;
  assistantExecutionMode?: "durable" | "in-process-compatibility";
  customDimensionLabels?: Partial<Record<Dimension, string>>;
}
export interface Quality {
  source: string;
  state: State;
  observedAt: string;
  watermark: string | null;
  warningCodes: string[];
}
export interface Count {
  value: number | null;
  state: State;
  source: string;
}
export interface Inventory {
  scope: Scope;
  state: State;
  observedAt: string;
  backends: Count;
  modelDeployments: Count;
  accessContracts: Count;
  mcpTools: Count;
  publishedAgents: Count;
  activeAlerts: Count;
  warnings: string[];
}
export interface Message {
  role: "user" | "assistant";
  content: string;
  timestamp: string;
}
export interface Conversation {
  id: string;
  title: string;
  messages: Message[];
  status: "idle" | "queued" | "running" | "failed";
  errorCode: string | null;
  updatedAt: string;
}
export interface Change {
  id: string;
  authorId: string;
  summary: string;
  preview: string;
  approvalHash: string;
  approvalId: string | null;
  approvalStatus: string;
  executionStatus: string;
  production: boolean;
  confirmationWord: string | null;
  pullRequestUrl: string | null;
}
export type Dimension = "model" | "deployment" | "product" | "application" | "backend" | "region"
  | "gateway" | "service_type" | "operation_name" | "ai_gateway_id" | "custom_dimension_1" | "custom_dimension_2";
export interface Query {
  window: { start: string; end: string };
  groupBy: Dimension;
  model?: string;
  product?: string;
  application?: string;
  dimensionFilters?: Partial<Record<Dimension, string | null>>;
}
export interface Usage {
  prompt: number;
  completion: number;
  cachedPrompt: number;
  total: number | null;
  promptAudio?: number;
  completionAudio?: number;
  completionReasoning?: number;
  completionAcceptedPrediction?: number;
  completionRejectedPrediction?: number;
  promptImage?: number;
  cachedPromptImage?: number;
}
export interface Money {
  amount: string;
  currency: string;
  basis: string;
}
export interface Aggregate {
  window: { start: string; end: string };
  dimensions: Partial<Record<Dimension, string | null>>;
  usage: Usage;
  costs: Money[];
  pricingCoverage: { pricedUsagePercent: string; warningCodes: string[] };
}
export interface FinOps {
  reportId?: string | null;
  sourceScanComplete?: boolean;
  inputKind?: "source-scan" | "read-model";
  readModelVersion?: string | null;
  quality: Quality;
  warnings: string[];
  requests: number;
  usage: Usage;
  costs: Money[];
  pricedUsagePercent: string;
  unpricedRequests: number;
  scannedRecords: number;
  rejectedRecords: number;
  truncated: boolean;
  aggregates: Aggregate[];
  models: string[];
  products: string[];
  applications: string[];
  filterOptions?: Partial<Record<Dimension, string[]>>;
  analysis?: FinOpsAnalysis | null;
}
export interface AnalyticalRow {
  window: Query["window"];
  dimensions: Partial<Record<Dimension, string | null>>;
  requests: number;
  unpricedRequests: number;
  usage: Usage;
  unavailableTokens: string[];
  costs: Money[];
  pricedUsagePercent: string;
  hourUtc?: number | null;
}
export interface FinOpsAnalysis {
  profiles: Record<string, AnalyticalRow[]>;
  customDimensions: Dimension[];
  hourOfDay: AnalyticalRow[];
}
export interface FinOpsReport {
  id: string;
  scope: Scope;
  query: Query;
  includeAnalysis?: boolean;
  customDimensions?: Dimension[];
  inputKind?: "source-scan" | "read-model";
  readModelVersion?: string | null;
  status: "pending" | "running" | "succeeded" | "failed" | "cancelled";
  scannedRecords: number;
  matchedRequests: number;
  rejectedRecords: number;
  updatedAt: string;
  expiresAt: string;
  pricingSnapshotAt: string;
  errorCode: string | null;
  result: FinOps | null;
}
export interface Operations {
  quality: Quality;
  warnings: string[];
  metrics: { name: string; value: string | null; unit: string; state: State }[];
  timeline: {
    timestamp: string;
    requests: number;
    failures: number;
    throttled: number;
    p95Ms: string | null;
  }[];
}
export type GatewayDimension = "backend" | "product_id" | "subscription_id" | "region" | "api_id" | "operation_name";
export interface OperationsQuery extends Query {
  gatewayFilters?: Partial<Record<GatewayDimension, string | null>>;
}
export interface RequestEvidenceResult {
  quality: Quality;
  warnings: string[];
  truncated: boolean;
  records: {
    timestamp: string; correlationId: string; responseCode: number | null; backendResponseCode: number | null;
    totalMs: string | null; backendMs: string | null; backend: string | null; productId: string | null;
    subscriptionId: string | null; region: string | null; apiId: string | null; operationName: string | null;
  }[];
}
export interface AlertInstances {
  quality: Quality; warnings: string[]; truncated: boolean;
  alerts: { id: string; severity: string; condition: string; state: string; startedAt: string; modifiedAt: string; portalUrl: string }[];
}
export interface PipelineHealth {
  quality: Quality; warnings: string[];
  components: { component: string; state: State; observedAt: string | null; metrics: Record<string, string>; warnings: string[]; portalUrl: string | null }[];
}
export interface OperationalAttribution {
  quality: Quality; warnings: string[]; readModelVersion: string; truncated: boolean;
  groups: Record<string, { value: string | null; requests: number }[]>;
}
