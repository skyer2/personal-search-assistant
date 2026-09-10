import type { MonitorMessage } from "../types";
import {
  canonicalEventName,
  isFastPathRun,
  projectPhaseStates,
  type PhaseLifecycleState
} from "./phaseProjection";

export { canonicalEventName };

export const PHASE_LABELS: Record<string, string> = {
  understand: "理解任务",
  strategy: "制定研究方向",
  research: "执行研究",
  coverage: "证据覆盖",
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
  "quality",
  "delivery"
] as const;

export type PipelinePhase = string;
export type PhaseTone =
  | "idle"
  | "running"
  | "paused"
  | "done"
  | "partial"
  | "insufficient"
  | "failed"
  | "skipped";

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

function toneForState(state: PhaseLifecycleState, paused: boolean): PhaseTone {
  if (state === "running") return paused ? "paused" : "running";
  if (state === "completed") return "done";
  if (state === "partial") return "partial";
  if (state === "insufficient") return "insufficient";
  if (state === "failed") return "failed";
  if (state === "skipped") return "skipped";
  return "idle";
}

function lastOfKind(events: MonitorMessage[], names: string[]): MonitorMessage | undefined {
  return [...events].reverse().find((event) => names.includes(canonicalEventName(event)));
}

function sourceEventForPhase(events: MonitorMessage[], phase: string): MonitorMessage | undefined {
  const researchKinds = ["worker.started", "worker.completed", "worker.failed"];
  const map: Record<string, string[]> = {
    understand: ["brief.compiled"],
    strategy: ["supervisor.started", "supervisor.decided", "plan.created", "worker.started"],
    research: researchKinds,
    fast_research: researchKinds,
    coverage: ["coverage.assessed"],
    fast_coverage: ["coverage.assessed"],
    synthesis: ["synthesis.started", "synthesis.completed", "synthesis.failed"],
    quality: ["quality.assessed"],
    delivery: ["run.terminated", "run.completed", "run.failed"]
  };
  return lastOfKind(events, map[phase] ?? []);
}

function stepHintForPhase(
  phase: string,
  state: PhaseLifecycleState,
  detail: string | undefined,
  source: MonitorMessage | undefined
): string {
  if (detail && ["research", "fast_research", "coverage", "fast_coverage", "quality"].includes(phase)) {
    return detail;
  }
  if (!source) return "";
  const data = source.data ?? {};
  return text(data.status) || text(data.execution_status) || text(data.result_status) || state;
}

export function buildPhaseTimeline(
  events: MonitorMessage[],
  options: { paused?: boolean } = {}
): PhaseTimelineItem[] {
  const paused = Boolean(options.paused);
  const fastPath = isFastPathRun(events);
  const stageOrder: string[] = fastPath ? [...FAST_PATH_ORDER] : [...PHASE_ORDER];
  const states = projectPhaseStates(events);

  return states
    .filter((state) => state.state !== "pending")
    .map((state) => {
      const source = sourceEventForPhase(events, state.phase);
      return {
        phase: state.phase,
        status: state.state,
        tone: toneForState(state.state, paused),
        durationMs: source ? asDuration(source.data?.duration_ms) : undefined,
        timestamp: source?.timestamp ?? "",
        data: source?.data ?? {},
        stepHint: stepHintForPhase(state.phase, state.state, state.detail, source)
      };
    })
    .filter((item) => stageOrder.includes(item.phase));
}

function terminalStatus(events: MonitorMessage[]): PhaseProgress["terminalStatus"] {
  const event = lastOfKind(events, ["run.terminated", "run.completed", "run.failed"]);
  if (!event) return "";
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
  return "";
}

export function formatPhaseStatus(phase: string, status: string): string {
  if (status === "pending") return "待开始";
  if (status === "running") return "进行中";
  if (status === "completed") return phase.includes("coverage") ? "充分" : "已完成";
  if (status === "partial") return phase === "delivery" ? "部分可确认" : "部分完成";
  if (status === "insufficient") return "不足";
  if (status === "failed") return phase === "quality" ? "未通过" : "失败";
  if (status === "skipped") return "未执行";
  return status;
}

export function computePhaseProgress(
  events: MonitorMessage[],
  options: { paused?: boolean } = {}
): PhaseProgress {
  const fastPath = isFastPathRun(events);
  const stageOrder: string[] = fastPath ? [...FAST_PATH_ORDER] : [...PHASE_ORDER];
  const items = buildPhaseTimeline(events, { paused: options.paused });
  const terminal = terminalStatus(events);
  const states = projectPhaseStates(events);
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
          ["fast_coverage", 80],
          ["quality", 95],
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
    if (["done", "failed", "partial", "insufficient"].includes(item.tone)) {
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
    states.some((state) => state.state === "failed") ||
    events.some((event) => {
      const kind = canonicalEventName(event);
      return ["worker.failed", "tool.failed", "synthesis.failed", "run.failed"].includes(kind) || event.event === "error";
    });

  return {
    percent,
    currentPhase: current?.phase ?? (items.length > 0 ? items[items.length - 1].phase : ""),
    currentLabel: current
      ? PHASE_LABELS[current.phase] ?? current.phase
      : terminal
        ? "流程已结束"
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
