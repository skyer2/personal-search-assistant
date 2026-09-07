import type { HitlInterruptPayload, MonitorMessage } from "../types";

export type RunStatus =
  | "idle"
  | "running"
  | "awaiting_approval"
  | "cancelling"
  | "completed"
  | "failed"
  | "recoverable"
  | "interrupted"
  | "partial";

export type RunOutcomeKind =
  | "execution_failed"
  | "quality_rejected"
  | "partially_confirmed"
  | "no_reliable_source";

export interface RunOutcomePresentation {
  kind: RunOutcomeKind;
  title: string;
  detail: string;
}

const NO_RELIABLE_SOURCE_REASONS = new Set([
  "insufficient_trusted_evidence",
  "no_evidence",
  "no_content",
  "no_reliable_source"
]);

function outcomeText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function describeRunOutcome(input: {
  runStatus: RunStatus;
  quality?: Record<string, unknown>;
  termination?: Record<string, unknown>;
}): RunOutcomePresentation | null {
  if (input.runStatus === "failed") {
    return {
      kind: "execution_failed",
      title: "执行失败",
      detail: "执行链路出现异常，本次没有生成可信结论。"
    };
  }
  if (input.runStatus !== "partial") {
    return null;
  }

  const qualityReason = outcomeText(input.quality?.reason);
  const reason = outcomeText(input.termination?.reason) || qualityReason;
  if (NO_RELIABLE_SOURCE_REASONS.has(reason)) {
    return {
      kind: "no_reliable_source",
      title: "无法找到可靠来源",
      detail: "质量门禁未放行：没有一手来源或两个独立高质量来源。"
    };
  }
  const qualityFailed =
    input.quality?.passed === false ||
    input.termination?.quality_passed === false ||
    Boolean(outcomeText(input.termination?.quality_reason));
  if (qualityFailed) {
    return {
      kind: "quality_rejected",
      title: "质量拒绝",
      detail: qualityReason ? `质量门禁拒绝低可信结论：${qualityReason}` : "质量门禁拒绝低可信结论。"
    };
  }
  return {
    kind: "partially_confirmed",
    title: "部分可确认",
    detail: "已保留可确认结论，但未达到完整交付标准。"
  };
}

export function deriveRunStatus(input: {
  isRunning: boolean;
  isCancelling?: boolean;
  hitlPending: HitlInterruptPayload | null;
  events: MonitorMessage[];
  result?: string;
  taskFailed?: boolean;
  serverStatus?: string;
}): RunStatus {
  if (input.serverStatus === "recoverable") {
    return "recoverable";
  }
  if (input.hitlPending || input.serverStatus === "awaiting_approval") {
    return "awaiting_approval";
  }
  if (input.isCancelling || input.serverStatus === "cancelling") {
    return "cancelling";
  }
  if (input.taskFailed || input.events.some((event) => event.event === "error") || input.serverStatus === "failed") {
    if (!input.isRunning) {
      return "failed";
    }
  }
  if (input.isRunning || input.serverStatus === "running" || input.serverStatus === "queued") {
    return "running";
  }
  if (input.serverStatus === "partial") {
    return "partial";
  }
  if (input.result || input.events.some((event) => event.event === "task_result") || input.serverStatus === "completed") {
    return "completed";
  }
  if (input.events.some((event) => event.event === "task_cancelled") || input.serverStatus === "interrupted") {
    return "interrupted";
  }
  return "idle";
}

export function isLiveRun(status: RunStatus): boolean {
  return status === "running" || status === "cancelling";
}

export function isPausedRun(status: RunStatus): boolean {
  return status === "awaiting_approval";
}

export function runStatusLabel(status: RunStatus): string {
  const labels: Record<RunStatus, string> = {
    idle: "待命",
    running: "运行中",
    awaiting_approval: "等待审批",
    cancelling: "正在取消",
    completed: "已完成",
    failed: "执行失败",
    recoverable: "可恢复",
    interrupted: "已中断",
    partial: "部分可确认"
  };
  return labels[status];
}
