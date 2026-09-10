import type { MonitorMessage } from "../types";

export type PhaseLifecycleState =
  | "pending"
  | "running"
  | "completed"
  | "partial"
  | "insufficient"
  | "failed"
  | "skipped";

export interface PhaseState {
  phase: string;
  state: PhaseLifecycleState;
  detail?: string;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
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

export function isFastPathRun(events: MonitorMessage[]): boolean {
  return events.some((event) => {
    if (canonicalEventName(event) !== "topology.decided") {
      return false;
    }
    return event.data?.eligible === true || text(event.data?.execution_path) === "fast_path";
  });
}

function hasEvent(events: MonitorMessage[], names: string[]): boolean {
  return events.some((event) => names.includes(canonicalEventName(event)));
}

function lastEvent(events: MonitorMessage[], names: MonitorMessage[] = []): MonitorMessage | undefined {
  return [...events].reverse().find((event) => names.includes(event));
}

function lastOfKind(events: MonitorMessage[], names: string[]): MonitorMessage | undefined {
  return [...events].reverse().find((event) => names.includes(canonicalEventName(event)));
}

function workerStatus(event: MonitorMessage): string {
  const data = event.data ?? {};
  return text(data.status) || text(data.execution_status) || text(data.result_status) || text(data.worker_status);
}

function terminalOutcome(events: MonitorMessage[]): "completed" | "partial" | "failed" | "cancelled" | "" {
  const terminal = lastOfKind(events, ["run.terminated", "run.completed", "run.failed"]);
  if (!terminal) return "";
  const termination = terminal.data?.termination;
  const terminationRecord =
    typeof termination === "object" && termination !== null ? (termination as Record<string, unknown>) : {};
  const raw =
    text(terminal.data?.outcome) ||
    text(terminal.data?.status) ||
    text(terminationRecord.outcome) ||
    text(terminationRecord.runtime_status);
  if (["completed", "success", "finished"].includes(raw)) return "completed";
  if (["partial", "degraded"].includes(raw)) return "partial";
  if (["failed", "error", "crashed"].includes(raw)) return "failed";
  if (["cancelled", "canceled", "interrupted"].includes(raw)) return "cancelled";
  return "";
}

export function projectPhaseStates(events: MonitorMessage[]): PhaseState[] {
  const fastPath = isFastPathRun(events);
  const order = fastPath
    ? ["understand", "fast_research", "fast_coverage", "quality", "delivery"]
    : ["understand", "strategy", "research", "coverage", "synthesis", "quality", "delivery"];
  const terminal = terminalOutcome(events);

  const understand: PhaseState = hasEvent(events, ["brief.compiled"])
    ? { phase: "understand", state: "completed" }
    : { phase: "understand", state: events.length > 0 ? "running" : "pending" };

  const strategy: PhaseState = hasEvent(events, ["supervisor.decided", "plan.created", "worker.started"])
    ? { phase: "strategy", state: "completed" }
    : hasEvent(events, ["supervisor.started"])
      ? { phase: "strategy", state: "running" }
      : { phase: "strategy", state: "pending" };

  const workerEvents = events.filter((event) =>
    ["worker.started", "worker.completed", "worker.failed"].includes(canonicalEventName(event))
  );
  const started = workerEvents.filter((event) => canonicalEventName(event) === "worker.started").length;
  const terminalWorkers = workerEvents.filter((event) => canonicalEventName(event) !== "worker.started");
  const succeeded = terminalWorkers.filter((event) => {
    const status = workerStatus(event).toLowerCase();
    return !["failed", "error", "partial", "stopped", "degraded", "warning"].includes(status);
  }).length;
  const partial = terminalWorkers.filter((event) => {
    const status = workerStatus(event).toLowerCase();
    return ["partial", "stopped", "degraded", "warning"].includes(status);
  }).length;
  const failed = terminalWorkers.filter((event) => canonicalEventName(event) === "worker.failed").length;
  const running = Math.max(0, started - terminalWorkers.length);
  const researchPhase = fastPath ? "fast_research" : "research";
  let research: PhaseState = { phase: researchPhase, state: "pending" };
  if (started > 0) {
    if (running > 0) {
      research = {
        phase: researchPhase,
        state: "running",
        detail: `${started} tasks · ${succeeded} completed · ${running} running`,
      };
    } else if (failed > 0 || partial > 0) {
      const parts = [
        `${started} tasks`,
        partial ? `${partial} partial` : "",
        failed ? `${failed} failed` : "",
      ].filter(Boolean);
      research = { phase: researchPhase, state: "partial", detail: parts.join(" · ") };
    } else {
      research = { phase: researchPhase, state: "completed", detail: `${started} tasks · ${succeeded} completed` };
    }
  }

  const coverageEvent = lastOfKind(events, ["coverage.assessed"]);
  const coveragePhase = fastPath ? "fast_coverage" : "coverage";
  let coverage: PhaseState = { phase: coveragePhase, state: "pending" };
  if (coverageEvent) {
    const status = (text(coverageEvent.data?.status) || "running").toLowerCase();
    if (["sufficient", "success", "completed", "ok", "done"].includes(status)) {
      coverage = { phase: coveragePhase, state: "completed", detail: "sufficient" };
    } else if (["gap", "insufficient", "partial", "warning"].includes(status)) {
      coverage = { phase: coveragePhase, state: "insufficient", detail: status };
    } else if (["start", "running"].includes(status)) {
      coverage = { phase: coveragePhase, state: "running" };
    }
  }

  const synthesisEvent = lastOfKind(events, ["synthesis.completed", "synthesis.failed", "synthesis.started"]);
  let synthesis: PhaseState = { phase: "synthesis", state: "pending" };
  if (fastPath) {
    synthesis = { phase: "synthesis", state: "skipped", detail: "fast path direct answer" };
  } else if (synthesisEvent) {
    const kind = canonicalEventName(synthesisEvent);
    synthesis =
      kind === "synthesis.failed"
        ? { phase: "synthesis", state: "failed" }
        : kind === "synthesis.started"
          ? { phase: "synthesis", state: "running" }
          : { phase: "synthesis", state: "completed" };
  } else if (terminal) {
    synthesis = { phase: "synthesis", state: "skipped" };
  }

  const qualityEvent = lastOfKind(events, ["quality.assessed"]);
  let quality: PhaseState = { phase: "quality", state: "pending" };
  if (qualityEvent) {
    const status = (text(qualityEvent.data?.status) || text(qualityEvent.data?.verdict) || "").toLowerCase();
    if (["pass", "passed", "success", "completed", "ok", "done"].includes(status)) {
      quality = { phase: "quality", state: "completed" };
    } else if (["fail", "failed", "quality_rejected", "error"].includes(status)) {
      quality = { phase: "quality", state: "failed", detail: "quality rejected" };
    } else if (["start", "running"].includes(status)) {
      quality = { phase: "quality", state: "running" };
    }
  } else if (terminal) {
    quality = { phase: "quality", state: "skipped" };
  }

  const delivery: PhaseState = terminal
    ? { phase: "delivery", state: terminal === "cancelled" ? "skipped" : terminal }
    : { phase: "delivery", state: "pending" };

  const byPhase = new Map([
    [understand.phase, understand],
    [research.phase, research],
    [coverage.phase, coverage],
    [synthesis.phase, synthesis],
    [quality.phase, quality],
    [delivery.phase, delivery],
  ]);
  if (!fastPath) {
    byPhase.set(strategy.phase, strategy);
  }
  return order.map((phase) => byPhase.get(phase) ?? { phase, state: "pending" });
}

export { lastEvent };
