/**
 * 【Fix 2026-08-18】API 请求统一从 ./config 导入 apiUrl / buildApiUrl。
 * 此前在此重复定义 apiUrl 并直接使用未 import 的 API_BASE_URL，
 * 运行时抛出 ReferenceError: API_BASE_URL is not defined。
 */
import { apiUrl, buildApiUrl } from "./config";
import type {
  CancelTaskResponse,
  CitationsResponse,
  EvalReport,
  FileListResponse,
  HitlResumeResponse,
  JsonlTraceResponse,
  LangfuseConfigResponse,
  LangfuseTraceResponse,
  RunEventsResponse,
  SearchMode,
  SessionBootstrap,
  SessionTracesResponse,
  TaskResponse,
  TraceTree,
  UploadResponse
} from "../types";

async function requestJson<T>(input: RequestInfo | URL, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const message =
      typeof payload === "object" && payload && "detail" in payload
        ? String(payload.detail)
        : `HTTP ${response.status}`;
    throw new Error(message);
  }

  return payload as T;
}

export async function startTask(
  query: string,
  threadId: string,
  options?: { mode?: SearchMode; projectId?: string; userId?: string }
): Promise<TaskResponse> {
  return requestJson<TaskResponse>(apiUrl("/api/task"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      query,
      thread_id: threadId,
      mode: options?.mode ?? "agent",
      user_id: options?.userId ?? "me",
      tenant_id: "local",
      project_id: options?.projectId ?? "Inbox"
    })
  });
}

export async function cancelTask(threadId: string): Promise<CancelTaskResponse> {
  return requestJson<CancelTaskResponse>(apiUrl(`/api/task/${encodeURIComponent(threadId)}/cancel`), {
    method: "POST"
  });
}

export async function uploadSessionFiles(
  files: File[],
  threadId: string
): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("thread_id", threadId);
  files.forEach((file) => formData.append("files", file));

  return requestJson<UploadResponse>(apiUrl("/api/upload"), {
    method: "POST",
    body: formData
  });
}

export async function listSessionFiles(path: string): Promise<FileListResponse> {
  return requestJson<FileListResponse>(buildApiUrl("/api/files", { path }));
}

export async function fetchSessionBootstrap(sessionId: string): Promise<SessionBootstrap> {
  return requestJson<SessionBootstrap>(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/bootstrap`));
}

export async function listSessionArtifacts(sessionId: string): Promise<FileListResponse> {
  return requestJson<FileListResponse>(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/artifacts`));
}

export async function fetchRunEvents(
  runId: string,
  options?: { afterSeq?: number; beforeSeq?: number; limit?: number }
): Promise<RunEventsResponse> {
  const params = new URLSearchParams();
  if (options?.afterSeq != null) {
    params.set("after_seq", String(options.afterSeq));
  }
  if (options?.beforeSeq != null) {
    params.set("before_seq", String(options.beforeSeq));
  }
  if (options?.limit != null) {
    params.set("limit", String(options.limit));
  }
  const query = params.toString();
  const path = `/api/runs/${encodeURIComponent(runId)}/events${query ? `?${query}` : ""}`;
  return requestJson<RunEventsResponse>(apiUrl(path));
}

export function getDownloadUrl(path: string, sessionId?: string, options?: { download?: boolean }): string {
  if (sessionId && path && !path.startsWith("/") && !path.includes(":\\")) {
    const params: Record<string, string> = { name: path };
    if (options?.download) {
      params.download = "1";
    }
    return buildApiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/download`, params).toString();
  }
  return buildApiUrl("/api/download", { path }).toString();
}

export async function resumeHitl(
  threadId: string,
  decisions: Array<{ type: "approve" | "reject" | "edit"; edited_action?: Record<string, unknown> }>
): Promise<HitlResumeResponse> {
  return requestJson<HitlResumeResponse>(apiUrl(`/api/task/${encodeURIComponent(threadId)}/resume`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decisions })
  });
}

export async function fetchEvalBaseline(): Promise<EvalReport> {
  return requestJson<EvalReport>(apiUrl("/api/eval/baseline"));
}

export async function fetchEvalLatest(): Promise<EvalReport> {
  return requestJson<EvalReport>(apiUrl("/api/eval/latest"));
}

export async function runEvalDryRun(): Promise<{ status: string; report_file?: string }> {
  return requestJson(apiUrl("/api/eval/run?dry_run=true&report_md=true"), { method: "POST" });
}

export async function fetchTraceTree(sessionId: string): Promise<{ session_id: string; tree: TraceTree; total: number }> {
  return requestJson(apiUrl(`/api/traces/tree/${encodeURIComponent(sessionId)}`));
}

export async function fetchJsonlTrace(sessionId: string, runId?: string): Promise<JsonlTraceResponse> {
  const params = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
  return requestJson<JsonlTraceResponse>(apiUrl(`/api/traces/jsonl/${encodeURIComponent(sessionId)}${params}`));
}

export async function fetchRunTrace(runId: string): Promise<JsonlTraceResponse> {
  return requestJson<JsonlTraceResponse>(apiUrl(`/api/runs/${encodeURIComponent(runId)}/trace`));
}

export async function fetchRunTraceSummary(runId: string): Promise<JsonlTraceResponse> {
  return requestJson<JsonlTraceResponse>(apiUrl(`/api/runs/${encodeURIComponent(runId)}/summary`));
}

export async function fetchRunLineage(runId: string, offset = 0, limit = 100): Promise<{ items: Array<Record<string, unknown>>; total: number }> {
  return requestJson(apiUrl(`/api/runs/${encodeURIComponent(runId)}/lineage?offset=${offset}&limit=${limit}`));
}

export async function fetchRunTree(runId: string): Promise<{ tree: TraceTree; total: number }> {
  return requestJson(apiUrl(`/api/runs/${encodeURIComponent(runId)}/tree`));
}

export async function fetchSessionTraces(sessionId: string): Promise<SessionTracesResponse> {
  return requestJson<SessionTracesResponse>(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/traces`));
}

export async function fetchCitations(sessionId: string): Promise<CitationsResponse> {
  return requestJson<CitationsResponse>(apiUrl(`/api/traces/citations/${encodeURIComponent(sessionId)}`));
}

export async function fetchLangfuseConfig(): Promise<LangfuseConfigResponse> {
  return requestJson<LangfuseConfigResponse>(apiUrl("/api/traces/langfuse/config"));
}

export async function fetchLangfuseTraces(sessionId: string): Promise<LangfuseTraceResponse> {
  return requestJson<LangfuseTraceResponse>(
    apiUrl(`/api/traces/langfuse/${encodeURIComponent(sessionId)}`)
  );
}
