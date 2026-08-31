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
  SearchMode,
  TaskResponse,
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
      mode: options?.mode ?? "auto",
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

export function getDownloadUrl(path: string): string {
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

export async function fetchJsonlTrace(sessionId: string): Promise<JsonlTraceResponse> {
  return requestJson<JsonlTraceResponse>(apiUrl(`/api/traces/jsonl/${encodeURIComponent(sessionId)}`));
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
