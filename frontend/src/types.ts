export type SearchMode = "auto" | "answer" | "search" | "research";

export type MonitorEventName =
  | "session_created"
  | "tool_start"
  | "assistant_call"
  | "task_result"
  | "task_cancelled"
  | "phase"
  | "hitl_interrupt"
  | "error"
  | string;

export type HarnessPhaseName =
  | "understand"
  | "plan"
  | "build_context"
  | "execute"
  | "compress"
  | "validate"
  | "recover"
  | "finalize"
  | "abort";

export type PhaseStatus = "start" | "done" | "failed" | "cancelled" | string;

export interface PhaseEventData {
  phase?: HarnessPhaseName | string;
  status?: PhaseStatus;
  duration_ms?: number;
  session_id?: string;
  [key: string]: unknown;
}

export interface MonitorMessage {
  type: "monitor_event";
  event: MonitorEventName;
  message: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface PongMessage {
  type: "pong";
  message: string;
}

export type SocketMessage = MonitorMessage | PongMessage;

export interface TaskResponse {
  status: "started" | string;
  thread_id: string;
}

export interface CancelTaskResponse {
  status: "cancelled" | "cancelling" | string;
  thread_id: string;
  message?: string;
}

export interface UploadResponse {
  status: "uploaded" | string;
  files: string[];
}

export interface OutputFile {
  name: string;
  type: "file" | string;
  path: string;
  size: number;
  mtime: number;
}

export interface FileListResponse {
  files?: OutputFile[];
  error?: string;
}

export interface UploadedItem {
  uid: string;
  name: string;
  size: number;
  raw: File;
}

export type WorkspaceTab = "chat" | "eval" | "trace";

export interface HitlActionRequest {
  name: string;
  args: Record<string, unknown>;
}

export interface HitlReviewConfig {
  action_name: string;
  allowed_decisions: string[];
}

export interface HitlInterruptPayload {
  session_id?: string;
  action_requests: HitlActionRequest[];
  review_configs: HitlReviewConfig[];
  step_index?: number;
  gate_type?: "step" | "interrupt_on" | "plan_review" | string;
  editable?: boolean;
}

export interface HitlResumeResponse {
  status: string;
  thread_id: string;
  decisions: Array<{ type: string; edited_action?: Record<string, unknown> }>;
}

export interface EvalReport {
  total?: number;
  passed?: number;
  mode?: string;
  generated_at?: string;
  report_file?: string;
  task_success_rate?: number;
  tool_selection_accuracy?: number;
  step_success_rate?: number;
  recovery_rate?: number;
  avg_tool_calls?: number;
  avg_latency_ms?: number;
  avg_compression_ratio?: number;
  memory_recall_hit_rate?: number;
  citation_coverage_rate?: number;
  hallucination_rate?: number;
  trajectory_similarity?: number;
  baseline_comparison?: {
    deltas?: Record<string, number | null>;
    regressions?: string[];
    blocked_merge?: boolean;
  };
  results?: Array<{
    task_id: string;
    success: boolean;
    status?: string;
    retry_count?: number;
  }>;
}

export interface JsonlTraceEvent {
  phase: string;
  status: string;
  timestamp?: string;
  step_index?: number;
  step_type?: string;
  duration_ms?: number;
  session_id?: string;
  evidence_sources?: EvidenceSource[];
  [key: string]: unknown;
}

export interface EvidenceSource {
  source_id: string;
  step_index: number;
  step_type: string;
  source_kind: string;
  locator: string;
  excerpt: string;
  timestamp?: string;
}

export interface CitationsResponse {
  session_id: string;
  sources: EvidenceSource[];
  total: number;
  generated_at?: string;
  message?: string;
}

export interface LangfuseTraceItem {
  id?: string;
  name?: string;
  sessionId?: string;
  session_id?: string;
  latency?: number;
  duration?: number;
  status?: string;
  level?: string;
}

export interface JsonlTraceResponse {
  session_id: string;
  events: JsonlTraceEvent[];
  total: number;
  message?: string;
}

export interface LangfuseTraceResponse {
  session_id: string;
  enabled: boolean;
  traces: LangfuseTraceItem[];
  message?: string;
  ui_url?: string;
}

export interface LangfuseConfigResponse {
  enabled: boolean;
  host: string;
  ui_url?: string | null;
}
