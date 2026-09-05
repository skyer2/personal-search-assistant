export type SearchMode = "agent" | "direct";

export type ConnectionState = "connecting" | "connected" | "reconnecting" | "closed";

export type MonitorEventName =
  | "session_created"
  | "tool_start"
  | "tool_end"
  | "tool_error"
  | "assistant_call"
  | "task_result"
  | "task_cancelled"
  | "phase"
  | "worker"
  | "progress"
  | "replan"
  | "evidence"
  | "plan"
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
  run_id?: string;
  session_id?: string;
  seq?: number;
  event_id?: string;
  replay?: boolean;
}

export interface PongMessage {
  type: "pong";
  message: string;
}

export interface ReplayCompleteMessage {
  type: "replay_complete";
  run_id?: string;
  after_seq?: number;
  count?: number;
}

export type SocketMessage = MonitorMessage | PongMessage | ReplayCompleteMessage;

export interface TaskResponse {
  status: "started" | string;
  thread_id: string;
  run_id?: string;
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
  raw?: File;
  uploadedAt?: string;
  serverFileId?: number | string;
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
  gate_pass_rate?: number;
  outcome_score?: number;
  grounding_score?: number;
  trajectory_score?: number;
  plan_validation_pass_rate?: number;
  replan_recovery_rate?: number;
  pass_at_1?: number;
  pass_at_k?: number;
  pass_hat_k?: number;
  latency_p95_ms?: number;
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
    failure_stage?: string;
    failure_type?: string;
  }>;
}

export interface JsonlTraceEvent {
  phase?: string;
  status?: string;
  timestamp?: string;
  step_index?: number;
  step_type?: string;
  duration_ms?: number;
  session_id?: string;
  run_id?: string;
  trace_id?: string;
  span_id?: string;
  parent_span_id?: string;
  type?: string;
  event?: string;
  task_id?: string;
  plan_version?: number;
  attempt?: number;
  evidence_sources?: EvidenceSource[];
  [key: string]: unknown;
}

export interface TraceSpanNode {
  span_id: string;
  parent_span_id?: string | null;
  name: string;
  phase?: string;
  status?: string;
  duration_ms?: number;
  task_id?: string;
  plan_version?: number;
  attempt?: number;
  timestamp?: string;
  children: TraceSpanNode[];
}

export interface TraceTree {
  roots: TraceSpanNode[];
  span_count: number;
  event_count: number;
  omitted_count?: number;
}

export interface TraceSummary {
  status?: string;
  started_at?: string | null;
  ended_at?: string | null;
  counts?: Record<string, number>;
  termination?: Record<string, unknown> | null;
  identity?: {
    session_id?: string;
    run_id?: string;
    trace_id?: string;
    git_sha?: string;
    config_hash?: string;
    variant?: string;
  };
  brief?: Record<string, unknown> | null;
  plans?: Array<Record<string, unknown>>;
  workers?: Array<Record<string, unknown>>;
  progress?: Array<Record<string, unknown>>;
  replans?: Array<Record<string, unknown>>;
  evidence?: Array<Record<string, unknown>>;
  synthesis?: Array<Record<string, unknown>>;
  recoveries?: Array<Record<string, unknown>>;
  quality?: Record<string, unknown>;
  lineage?: Array<Record<string, unknown>>;
  evals?: Array<Record<string, unknown>>;
  eval_matrix?: Array<{
    variants?: string[];
    cases?: Array<{
      case_id?: string;
      variants?: Record<string, Record<string, unknown>>;
    }>;
  }>;
  failures?: Array<Record<string, unknown>>;
  failure_counts?: Record<string, number>;
  failure_origin?: Record<string, unknown> | null;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    cache_read_tokens?: number;
    cost_usd?: number;
    calls?: number;
  };
  event_count?: number;
  worker_count?: number;
  progress_count?: number;
  replan_count?: number;
  gap_closure_rate?: number | null;
  replan_useful?: boolean | null;
  replan_attempted?: boolean;
  progress_attempted?: boolean;
  trace_integrity?: {
    passed?: boolean | null;
    issues?: string[];
    counts?: Record<string, number>;
    is_agent_mode?: boolean;
    span_tree?: {
      span_count?: number;
      root_count?: number;
      cycle_count?: number;
      valid?: boolean;
    };
  };
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
  run_id?: string;
  events?: JsonlTraceEvent[];
  total: number;
  message?: string;
  tree?: TraceTree;
  summary?: TraceSummary;
  scope?: string;
}

export interface SessionTraceItem {
  run_id: string;
  session_id?: string;
  status?: string;
  query?: string;
  created_at?: string;
  started_at?: string;
  ended_at?: string;
  last_event_seq?: number;
}

export interface SessionTracesResponse {
  session_id: string;
  traces: SessionTraceItem[];
  current_run_id?: string | null;
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

export type ServerRunStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "cancelling"
  | "completed"
  | "failed"
  | "partial"
  | "recoverable"
  | "interrupted"
  | string;

export interface RunSnapshot {
  run_id: string;
  session_id: string;
  query: string;
  status: ServerRunStatus;
  mode?: string;
  created_at?: string;
  started_at?: string | null;
  ended_at?: string | null;
  current_phase?: string | null;
  plan_version?: number | null;
  final_result?: string;
  error?: string;
  session_workspace?: string;
  last_event_seq?: number;
  hitl_status?: string | null;
  hitl_payload?: HitlInterruptPayload | null;
  paused_total_ms?: number;
  pause_started_at?: string | null;
  tool_calls?: number;
  assistant_calls?: number;
  errors?: number;
  elapsed_ms?: number;
}

export interface SessionBootstrap {
  found: boolean;
  session_id: string;
  runs: RunSnapshot[];
  current_run: RunSnapshot | null;
  hitl: HitlInterruptPayload | null;
  uploaded_files: Array<{
    name: string;
    size: number;
    uploaded_at?: string;
    server_file_id?: number | string;
  }>;
  output_files: OutputFile[];
  stats: {
    tool_calls: number;
    assistant_calls: number;
    errors: number;
  };
  last_event_seq: number;
  events: MonitorMessage[];
  notice?: string;
}

export interface RunEventsResponse {
  run_id: string;
  session_id: string;
  events: MonitorMessage[];
  last_event_seq: number;
  count: number;
}
