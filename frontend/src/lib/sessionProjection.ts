import type { ChatTurn } from "../components/ConversationThread";
import type { HitlInterruptPayload, MonitorMessage, RunSnapshot, SessionBootstrap, UploadedItem } from "../types";
import {
  IDLE_ELAPSED_CLOCK,
  type ElapsedClockState
} from "./elapsedClock";

const ACTIVE_SERVER = new Set(["queued", "running", "awaiting_approval", "cancelling"]);

export function eventDedupeKey(message: MonitorMessage): string | null {
  const runId = message.run_id || (typeof message.data?.run_id === "string" ? message.data.run_id : "");
  const seq = message.seq ?? (typeof message.data?.seq === "number" ? message.data.seq : null);
  const eventId = message.event_id || (typeof message.data?.event_id === "string" ? message.data.event_id : "");
  if (runId && seq != null) {
    return `${runId}:${seq}`;
  }
  if (eventId) {
    return eventId;
  }
  return null;
}

export function mergeMonitorEvents(
  current: MonitorMessage[],
  incoming: MonitorMessage[],
  seen: Set<string>,
  windowSize: number
): MonitorMessage[] {
  const next = [...current];
  incoming.forEach((message) => {
    const key = eventDedupeKey(message);
    if (key) {
      if (seen.has(key)) {
        return;
      }
      seen.add(key);
    }
    next.push(message);
  });
  return next.slice(-windowSize);
}

export function clockFromRun(run: RunSnapshot | null): ElapsedClockState {
  if (!run?.started_at) {
    return IDLE_ELAPSED_CLOCK;
  }
  const startedAtMs = Date.parse(run.started_at);
  if (Number.isNaN(startedAtMs)) {
    return IDLE_ELAPSED_CLOCK;
  }
  return {
    startedAtMs,
    endedAtMs: run.ended_at ? Date.parse(run.ended_at) : null,
    pauseStartedAtMs: run.pause_started_at ? Date.parse(run.pause_started_at) : null,
    pausedAccumulatedMs: run.paused_total_ms || 0
  };
}

export function hitlFromBootstrap(data: SessionBootstrap): HitlInterruptPayload | null {
  const payload = data.hitl || data.current_run?.hitl_payload || null;
  if (!payload) {
    return null;
  }
  if (!Array.isArray(payload.action_requests) || payload.action_requests.length === 0) {
    return payload;
  }
  return payload;
}

export function uploadsFromBootstrap(data: SessionBootstrap): UploadedItem[] {
  return (data.uploaded_files || []).map((file, index) => ({
    uid: String(file.server_file_id ?? `${file.name}-${index}`),
    name: file.name,
    size: file.size || 0,
    uploadedAt: file.uploaded_at,
    serverFileId: file.server_file_id
  }));
}

export function turnsFromBootstrap(data: SessionBootstrap): ChatTurn[] {
  return (data.runs || []).map((run) => runToTurn(run, data));
}

export function runToTurn(run: RunSnapshot, data: SessionBootstrap): ChatTurn {
  const active = ACTIVE_SERVER.has(String(run.status));
  const isCurrent = data.current_run?.run_id === run.run_id;
  return {
    id: run.run_id,
    content: run.query,
    events: isCurrent ? data.events || [] : [],
    files: isCurrent ? data.output_files || [] : [],
    isRunning: active,
    result: run.final_result || run.error || "",
    timestamp: run.started_at || run.created_at || new Date().toISOString(),
    elapsedClock: runToElapsedClock(run)
  };
}

function runToElapsedClock(run: RunSnapshot): ElapsedClockState {
  if (run.started_at) {
    return clockFromRun(run);
  }
  const elapsedMs = run.elapsed_ms || 0;
  if (elapsedMs <= 0) {
    return IDLE_ELAPSED_CLOCK;
  }
  return {
    startedAtMs: 0,
    endedAtMs: elapsedMs,
    pauseStartedAtMs: null,
    pausedAccumulatedMs: 0
  };
}

export function isActiveServerStatus(status?: string | null): boolean {
  return ACTIVE_SERVER.has(String(status || ""));
}
