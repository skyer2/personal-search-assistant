import type { HitlInterruptPayload, MonitorMessage } from "../types";

export type RunStatus =
  | "idle"
  | "running"
  | "awaiting_approval"
  | "cancelling"
  | "completed"
  | "failed"
  | "recoverable"
  | "partial";

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
    return "idle";
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
    failed: "失败",
    recoverable: "可恢复",
    partial: "部分完成"
  };
  return labels[status];
}
