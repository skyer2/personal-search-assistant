import type { MonitorMessage } from "../types";

export const PHASE_LABELS: Record<string, string> = {
  understand: "理解任务",
  plan: "生成计划",
  build_context: "构建上下文",
  execute: "执行",
  compress: "压缩上下文",
  validate: "结果校验",
  recover: "失败恢复",
  finalize: "完成交付",
  abort: "中止"
};

export const PHASE_ORDER = [
  "understand",
  "plan",
  "build_context",
  "execute",
  "compress",
  "validate",
  "finalize"
] as const;

export type PipelinePhase = (typeof PHASE_ORDER)[number];

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
  hasFailed: boolean;
}

function asDuration(value: unknown): number | undefined {
  const raw = typeof value === "number" ? value : typeof value === "string" ? Number(value) : undefined;
  return Number.isFinite(raw) ? raw : undefined;
}

function stepHintFromData(data: Record<string, unknown>): string {
  const stepType = typeof data.step_type === "string" ? data.step_type : "";
  const stepIndex = typeof data.step_index === "number" ? data.step_index : null;
  const totalSteps = typeof data.total_steps === "number" ? data.total_steps : null;
  if (stepIndex !== null && totalSteps !== null && totalSteps > 0) {
    const label = stepType || "step";
    return `Step ${stepIndex + 1}/${totalSteps} · ${label}`;
  }
  return stepType;
}

function toneForStatus(status: string, paused: boolean): PhaseTone {
  if (status === "failed" || status === "cancelled") return "failed";
  if (status === "done") return "done";
  if (status === "start") return paused ? "paused" : "running";
  return "idle";
}

export function buildPhaseTimeline(
  events: MonitorMessage[],
  options: { paused?: boolean } = {}
): PhaseTimelineItem[] {
  const latestByPhase = new Map<string, PhaseTimelineItem>();
  const paused = Boolean(options.paused);

  for (const event of events) {
    if (event.event !== "phase") continue;
    const phase = String(event.data.phase ?? "");
    const status = String(event.data.status ?? "");
    if (!phase) continue;

    latestByPhase.set(phase, {
      phase,
      status,
      tone: toneForStatus(status, paused),
      durationMs: asDuration(event.data.duration_ms),
      timestamp: event.timestamp,
      data: event.data,
      stepHint: stepHintFromData(event.data)
    });
  }

  const ordered = PHASE_ORDER.filter((phase) => latestByPhase.has(phase)).map(
    (phase) => latestByPhase.get(phase)!
  );

  for (const [phase, item] of latestByPhase) {
    if (!PHASE_ORDER.includes(phase as PipelinePhase)) {
      ordered.push(item);
    }
  }

  return ordered;
}

export function computePhaseProgress(
  events: MonitorMessage[],
  options: { paused?: boolean; completed?: boolean } = {}
): PhaseProgress {
  let items = buildPhaseTimeline(events, { paused: options.paused });
  const done = items.filter((item) => item.tone === "done").length;
  const failed = items.some((item) => item.tone === "failed");
  const current =
    items.find((item) => item.tone === "running" || item.tone === "paused") ??
    [...items].reverse().find((item) => item.tone === "done" || item.tone === "failed");

  const totalCount = PHASE_ORDER.length;
  const runningCredit = current && (current.tone === "running" || current.tone === "paused") ? 0.45 : 0;
  let percent = Math.round(((done + runningCredit) / totalCount) * 100);

  if (options.completed) {
    percent = 100;
    items = PHASE_ORDER.map((phase) => {
      const existing = items.find((item) => item.phase === phase);
      return {
        phase,
        status: "done",
        tone: "done" as const,
        durationMs: existing?.durationMs,
        timestamp: existing?.timestamp ?? "",
        data: existing?.data ?? {},
        stepHint: ""
      };
    });
  } else if (failed && percent < 8) {
    percent = 8;
  }

  percent = Math.max(0, Math.min(100, percent));

  const currentPhase = options.completed
    ? "finalize"
    : current?.phase ?? (events.length > 0 ? "understand" : "");
  const currentLabel = currentPhase ? PHASE_LABELS[currentPhase] ?? currentPhase : "等待开始";

  return {
    percent,
    currentPhase,
    currentLabel,
    stepHint: options.completed ? "" : current?.stepHint || "",
    completedCount: options.completed ? totalCount : done,
    totalCount,
    items,
    hasFailed: failed
  };
}
