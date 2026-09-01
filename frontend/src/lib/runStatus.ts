import type { HitlInterruptPayload, MonitorMessage } from "../types";

export type RunStatus =
  | "idle"
  | "running"
  | "awaiting_approval"
  | "cancelling"
  | "completed"
  | "failed";

export function deriveRunStatus(input: {
  isRunning: boolean;
  isCancelling?: boolean;
  hitlPending: HitlInterruptPayload | null;
  events: MonitorMessage[];
  result?: string;
  taskFailed?: boolean;
}): RunStatus {
  if (input.hitlPending) {
    return "awaiting_approval";
  }
  if (input.isCancelling) {
    return "cancelling";
  }
  if (input.taskFailed || input.events.some((event) => event.event === "error")) {
    if (!input.isRunning) {
      return "failed";
    }
  }
  if (input.isRunning) {
    return "running";
  }
  if (input.result || input.events.some((event) => event.event === "task_result")) {
    return "completed";
  }
  if (input.events.some((event) => event.event === "task_cancelled")) {
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
    failed: "失败"
  };
  return labels[status];
}
