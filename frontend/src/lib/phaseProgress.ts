import type { MonitorMessage } from "../types";

export const PHASE_LABELS: Record<string, string> = {
  understand: "理解任务",
  strategy: "制定研究方向",
  research: "执行研究",
  coverage: "证据整理与覆盖判断",
  synthesis: "结果合成",
  quality: "质量检查",
  delivery: "完成交付",
  fast_research: "快速检索",
  fast_coverage: "证据校验"
};

export const PHASE_ORDER = [
  "understand",
  "strategy",
  "research",
  "coverage",
  "synthesis",
  "quality",
  "delivery"
] as const;

export const FAST_PATH_ORDER = [
  "understand",
  "fast_research",
  "fast_coverage",
  "delivery"
] as const;

export type PipelinePhase = string;
export type PhaseTone = "idle" | "running" | "paused" | "done" | "failed";

export interface PhaseTimelineItem {
  phase: string;
  status: string;
  tone: PhaseTone;
  durationMs?: number;
  timestamp: string;
  data: Record<string, unknown>;
  stepHint?: string;
}

export interface PhaseProgress {
  percent: number;
  currentPhase: string;
  currentLabel: string;
  stepHint: string;
  completedCount: number;
  totalCount: number;
  items: PhaseTimelineItem[];
  stageOrder: string[];
  hasFailed: boolean;
  terminalStatus: "completed" | "partial" | "failed" | "cancelled" | "";
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asDuration(value: unknown): number | undefined {
  const raw = typeof value === "number" ? value : typeof value === "string" ? Number(value) : undefined;
  return Number.isFinite(raw) ? raw : undefined;
}

export function canonicalEventName(message: MonitorMessage): string {
  const canonical = text(message.data?.canonical_event);
  if (canonical) {
    return canonical;
  }
  switch (message.event) {
    case "brief":
      return "brief.compiled";
    case "topology":
      return "topology.decided";
    case "plan":
      return "plan.created";
    case "coverage":
      return "coverage.assessed";
    case "finding":
      return "finding.compressed";
    case "evidence":
      return "evidence.registered";
    case "termination":
      return "run.terminated";
    case "task_result":
      return "run.completed";
    case "quality":
      return "quality.assessed";
    case "tool_start":
      return "tool.started";
    case "tool_end":
      return "tool.completed";
    case "tool_error":
      return "tool.failed";
    case "supervisor":
      return text(message.data?.status) === "start" || /\[supervisor\] start/.test(message.message)
        ? "supervisor.started"
        : "supervisor.decided";
    case "worker": {
      const status = text(message.data?.status) || text(message.data?.worker_status);
      if (status === "start") return "worker.started";
      if (["failed", "error"].includes(status)) return "worker.failed";
      return "worker.completed";
    }
    case "synthesis":
      return text(message.data?.status) === "start" || /\[synthesis\] start/.test(message.message)
        ? "synthesis.started"
        : text(message.data?.status) === "failed" || /\[synthesis\] failed/.test(message.message)
          ? "synthesis.failed"
          : "synthesis.completed";
    default:
      return message.event;
  }
}

function terminalStatus(events: MonitorMessage[]): PhaseProgress["terminalStatus"] {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    const kind = canonicalEventName(event);
    if (!["run.terminated", "run.completed", "run.failed"].includes(kind)) {
      continue;
    }
    const termination = event.data?.termination;
    const terminationRecord =
      typeof termination === "object" && termination !== null ? (termination as Record<string, unknown>) : {};
    const raw =
      text(event.data?.outcome) ||
      text(event.data?.status) ||
      text(terminationRecord.outcome) ||
      text(terminationRecord.runtime_status);
    if (["completed", "success", "finished"].includes(raw)) return "completed";
    if (["partial", "degraded"].includes(raw)) return "partial";
    if (["failed", "error", "crashed"].includes(raw)) return "failed";
    if (["cancelled", "canceled", "interrupted"].includes(raw)) return "cancelled";
  }
  return "";
}

function isFastPath(events: MonitorMessage[]): boolean {
  return events.some((event) => {
    if (canonicalEventName(event) !== "topology.decided") {
      return false;
    }
    return event.data?.eligible === true || text(event.data?.execution_path) === "fast_path";
  });
}

function stepHintFromData(data: Record<string, unknown>): string {
  const workerStatus = text(data.execution_status) || text(data.worker_status) || text(data.status);
  const taskId = text(data.task_id);
  if (workerStatus && taskId) {
    return `${taskId} · ${workerStatus}`;
  }
  return text(data.status);
}

function toneForStatus(status: string, paused: boolean): PhaseTone {
  if (["failed", "error", "fail"].includes(status)) return "failed";
  if (["done", "ok", "sufficient", "success", "completed"].includes(status)) return "done";
  if (["start", "running", "gap", "partial"].includes(status)) return paused ? "paused" : "running";
  return "idle";
}

export function buildPhaseTimeline(
  events: MonitorMessage[],
  options: { paused?: boolean } = {}
): PhaseTimelineItem[] {
  const paused = Boolean(options.paused);
  const fastPath = isFastPath(events);
  const stageOrder: string[] = fastPath ? [...FAST_PATH_ORDER] : [...PHASE_ORDER];
  const stageByEvent = new Map<string, string>([
    ["brief.compiled", "understand"],
    ["supervisor.started", "strategy"],
    ["supervisor.decided", "strategy"],
    ["plan.created", "strategy"],
    ["worker.started", fastPath ? "fast_research" : "research"],
    ["worker.completed", fastPath ? "fast_research" : "research"],
    ["worker.failed", fastPath ? "fast_research" : "research"],
    ["evidence.registered", fastPath ? "fast_coverage" : "coverage"],
    ["finding.compressed", fastPath ? "fast_coverage" : "coverage"],
    ["progress.assessed", fastPath ? "fast_coverage" : "coverage"],
    ["coverage.assessed", fastPath ? "fast_coverage" : "coverage"],
    ["synthesis.started", "synthesis"],
    ["synthesis.completed", "synthesis"],
    ["synthesis.failed", "synthesis"],
    ["quality.assessed", "quality"],
    ["run.terminated", "delivery"],
    ["run.completed", "delivery"],
    ["run.failed", "delivery"]
  ]);

  const latestByStage = new Map<string, PhaseTimelineItem>();
  for (const event of events) {
    const kind = canonicalEventName(event);
    const stage = stageByEvent.get(kind);
    if (!stage || !stageOrder.includes(stage)) {
      continue;
    }
    const status =
      text(event.data?.status) ||
      text(event.data?.execution_status) ||
      text(event.data?.worker_status) ||
      (kind.endsWith(".started") ? "start" : "done");
    const effectiveStatus =
      kind === "supervisor.decided" && !["failed", "error", "cancelled"].includes(status)
        ? "done"
        : status;
    latestByStage.set(stage, {
      phase: stage,
      status: effectiveStatus,
      tone: toneForStatus(effectiveStatus, paused),
      durationMs: asDuration(event.data?.duration_ms),
      timestamp: event.timestamp,
      data: event.data,
      stepHint: stepHintFromData(event.data)
    });
  }

  return stageOrder
    .map((stage) => latestByStage.get(stage))
    .filter((item): item is PhaseTimelineItem => Boolean(item));
}

export function computePhaseProgress(
  events: MonitorMessage[],
  options: { paused?: boolean } = {}
): PhaseProgress {
  const fastPath = isFastPath(events);
  const stageOrder: string[] = fastPath ? [...FAST_PATH_ORDER] : [...PHASE_ORDER];
  const items = buildPhaseTimeline(events, { paused: options.paused });
  const terminal = terminalStatus(events);
  const workerStarted = events.filter((event) => canonicalEventName(event) === "worker.started").length;
  const workerTerminal = events.filter((event) =>
    ["worker.completed", "worker.failed"].includes(canonicalEventName(event))
  ).length;
  const maxWorkerRatio = workerStarted > 0 ? Math.min(1, workerTerminal / workerStarted) : 0;
  const thresholds = new Map<string, number>(
    fastPath
      ? [
          ["understand", 20],
          ["fast_research", 60],
          ["fast_coverage", 85],
          ["delivery", 100]
        ]
      : [
          ["understand", 10],
          ["strategy", 20],
          ["research", 25 + Math.round(45 * maxWorkerRatio)],
          ["coverage", 75],
          ["synthesis", 82],
          ["quality", 96],
          ["delivery", 100]
        ]
  );

  let percent = 0;
  for (const item of items) {
    const threshold = thresholds.get(item.phase) ?? 0;
    if (item.tone === "done" || item.tone === "failed") {
      percent = Math.max(percent, threshold);
    } else if (item.tone === "running" || item.tone === "paused") {
      percent = Math.max(percent, Math.round(threshold * 0.9));
    }
  }
  if (terminal === "completed" || terminal === "partial") {
    percent = 100;
  }
  percent = Math.max(0, Math.min(100, percent));

  const current = [...items].reverse().find((item) => item.tone === "running" || item.tone === "paused");
  const failed =
    terminal === "failed" ||
    events.some((event) => {
      const kind = canonicalEventName(event);
      return ["worker.failed", "tool.failed", "synthesis.failed", "run.failed"].includes(kind) || event.event === "error";
    });

  return {
    percent,
    currentPhase: current?.phase ?? (items.length > 0 ? items[items.length - 1].phase : ""),
    currentLabel: current
      ? PHASE_LABELS[current.phase] ?? current.phase
      : events.length > 0
        ? "研究执行中"
        : "等待开始",
    stepHint: current?.stepHint || "",
    completedCount: items.filter((item) => item.tone === "done").length,
    totalCount: stageOrder.length,
    items,
    stageOrder,
    hasFailed: failed,
    terminalStatus: terminal
  };
}
