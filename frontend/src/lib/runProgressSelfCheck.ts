import { elapsedClockSelfCheck } from "./elapsedClock";
import { deriveRunStatus } from "./runStatus";
import { computePhaseProgress } from "./phaseProgress";
import type { MonitorMessage } from "../types";

function event(name: MonitorMessage["event"], data: Record<string, unknown> = {}): MonitorMessage {
  return {
    type: "monitor_event",
    event: name,
    message: name,
    data,
    timestamp: "2026-08-28T06:40:00.000Z"
  };
}

export function runProgressSelfCheck(): string[] {
  const errors: string[] = [];
  const runningEvents = [
    event("phase", { phase: "understand", status: "done" }),
    event("phase", { phase: "plan", status: "done" }),
    event("phase", { phase: "execute", status: "start", step_index: 0, total_steps: 3, step_type: "database_query" })
  ];

  const running = deriveRunStatus({
    isRunning: true,
    hitlPending: null,
    events: runningEvents
  });
  if (running !== "running") errors.push(`expected running, got ${running}`);

  const paused = deriveRunStatus({
    isRunning: true,
    hitlPending: {
      action_requests: [{ name: "database_query", args: {} }],
      review_configs: []
    },
    events: [...runningEvents, event("hitl_interrupt")]
  });
  if (paused !== "awaiting_approval") errors.push(`expected awaiting_approval, got ${paused}`);

  const liveProgress = computePhaseProgress(runningEvents);
  const pausedProgress = computePhaseProgress([...runningEvents, event("hitl_interrupt")], { paused: true });
  if (liveProgress.percent <= 0) errors.push("running percent should be > 0");
  if (pausedProgress.items.find((item) => item.phase === "execute")?.tone !== "paused") {
    errors.push("execute step should freeze as paused during HITL");
  }
  if (pausedProgress.percent !== liveProgress.percent) {
    errors.push("HITL pause must not rewind or animate progress");
  }
  const completed = computePhaseProgress(runningEvents, { completed: true });
  if (completed.percent !== 100) errors.push("completed percent should be 100");
  if (completed.items.some((item) => item.tone !== "done")) {
    errors.push("completed pipeline should mark every phase done");
  }

  const elapsedErrors = elapsedClockSelfCheck();
  errors.push(...elapsedErrors.map((error) => `elapsed: ${error}`));
  return errors;
}
