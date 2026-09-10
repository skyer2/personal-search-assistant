import { canonicalEventName } from "./phaseProgress";
import { eventBelongsToRun } from "./sessionProjection";
import type { MonitorMessage } from "../types";

export interface RunStatsProjection {
  toolCalls: number;
  toolFailed: number;
  workerRuns: number;
  workerSucceeded: number;
  workerPartial: number;
  workerFailed: number;
  runtimeFailed: number;
  providerFailed: number;
  fileCount: number;
}

export interface RunSourceStats {
  webSearchQueries: number;
  evidenceSources: number;
  webSources: number;
  uploadedFiles: number;
  databaseSources: number;
  primarySources: number;
  highQualitySecondarySources: number;
  otherSecondarySources: number;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function workerOutcome(message: MonitorMessage): "succeeded" | "partial" | "failed" | "skipped" | "" {
  const kind = canonicalEventName(message);
  if (kind === "worker.failed") {
    return "failed";
  }
  if (kind !== "worker.completed") {
    return "";
  }
  const execution = text(message.data?.execution_status);
  const result = text(message.data?.result_status);
  const status = text(message.data?.status) || text(message.data?.worker_status);
  if (result === "partial" || execution === "stopped" || status === "partial") {
    return "partial";
  }
  if (execution === "skipped" || status === "skipped") {
    return "skipped";
  }
  if (execution === "succeeded" || status === "done" || status === "ok") {
    return "succeeded";
  }
  return "succeeded";
}

export function deriveRunStats(
  events: MonitorMessage[],
  options: { fileCount?: number; runId?: string } = {}
): RunStatsProjection {
  const stats: RunStatsProjection = {
    toolCalls: 0,
    toolFailed: 0,
    workerRuns: 0,
    workerSucceeded: 0,
    workerPartial: 0,
    workerFailed: 0,
    runtimeFailed: 0,
    providerFailed: 0,
    fileCount: Math.max(0, Number(options.fileCount || 0))
  };

  const runId = options.runId;
  const scopedEvents = runId ? events.filter((event) => eventBelongsToRun(event, runId)) : events;
  for (const event of scopedEvents) {
    const kind = canonicalEventName(event);
    if (kind === "tool.started") {
      stats.toolCalls += 1;
    }
    if (kind === "tool.failed") {
      stats.toolFailed += 1;
    }
    if (kind === "worker.started") {
      stats.workerRuns += 1;
    }
    const outcome = workerOutcome(event);
    if (outcome === "succeeded") stats.workerSucceeded += 1;
    if (outcome === "partial") stats.workerPartial += 1;
    if (outcome === "failed") stats.workerFailed += 1;
    if (outcome === "failed" && text(event.data?.fail_reason).startsWith("provider_")) {
      stats.providerFailed += 1;
    }
    if (kind === "run.failed" || kind === "observability.internal_error" || event.event === "error") {
      stats.runtimeFailed += 1;
    }
  }
  return stats;
}

export function deriveRunSources(
  events: MonitorMessage[],
  options: { runId?: string } = {}
): RunSourceStats {
  const stats: RunSourceStats = {
    webSearchQueries: 0,
    evidenceSources: 0,
    webSources: 0,
    uploadedFiles: 0,
    databaseSources: 0,
    primarySources: 0,
    highQualitySecondarySources: 0,
    otherSecondarySources: 0
  };
  const evidenceIds = new Set<string>();

  const runId = options.runId;
  const scopedEvents = runId ? events.filter((event) => eventBelongsToRun(event, runId)) : events;
  for (const event of scopedEvents) {
    const kind = canonicalEventName(event);
    if (kind === "tool.started") {
      const toolName = text(event.data?.tool_name).toLowerCase();
      if (toolName.includes("search")) {
        stats.webSearchQueries += 1;
      }
      continue;
    }
    if (kind !== "evidence.registered") {
      continue;
    }

    const evidenceId = text(event.data?.evidence_id) || text(event.data?.source_id);
    if (evidenceId && evidenceIds.has(evidenceId)) {
      continue;
    }
    if (evidenceId) {
      evidenceIds.add(evidenceId);
      stats.evidenceSources += 1;
    }

    const sourceKind = text(event.data?.source_kind).toLowerCase();
    if (sourceKind === "web" || sourceKind === "url") stats.webSources += 1;
    if (sourceKind === "file" || sourceKind === "uploaded_file") stats.uploadedFiles += 1;
    if (["sql", "database", "kb", "knowledge_base"].includes(sourceKind)) stats.databaseSources += 1;

    const tier = text(event.data?.source_tier || event.data?.source_quality).toUpperCase();
    if (tier === "PRIMARY") stats.primarySources += 1;
    else if (tier === "HIGH_QUALITY_SECONDARY") stats.highQualitySecondarySources += 1;
    else stats.otherSecondarySources += 1;
  }
  return stats;
}
