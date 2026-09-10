import { elapsedClockSelfCheck } from "./elapsedClock";
import { computePhaseProgress, formatPhaseStatus } from "./phaseProgress";
import { projectPhaseStates } from "./phaseProjection";
import { deriveRunStatus } from "./runStatus";
import { deriveRunSources, deriveRunStats } from "./runStats";
import { eventBelongsToRun } from "./sessionProjection";
import type { MonitorMessage } from "../types";

function event(
  name: MonitorMessage["event"],
  data: Record<string, unknown> = {},
  runId = "run-current"
): MonitorMessage {
  return {
    type: "monitor_event",
    event: name,
    message: name,
    data,
    timestamp: "2026-09-10T06:40:00.000Z",
    run_id: runId
  };
}

function checkRunningProgress(): string[] {
  const errors: string[] = [];
  const runningEvents = [
    event("brief", { status: "ok", canonical_event: "brief.compiled" }),
    event("topology", { eligible: false, canonical_event: "topology.decided" }),
    event("supervisor", { status: "start", canonical_event: "supervisor.started" }),
    event("supervisor", { status: "decided", canonical_event: "supervisor.decided" }),
    event("worker", { status: "start", task_id: "w1", canonical_event: "worker.started" }),
    event("worker", { status: "start", task_id: "w2", canonical_event: "worker.started" }),
    event("worker", {
      status: "succeeded",
      execution_status: "succeeded",
      task_id: "w1",
      canonical_event: "worker.completed"
    })
  ];
  const progress = computePhaseProgress(runningEvents);
  if (progress.percent <= 0) errors.push("semantic running percent should be > 0");
  if (progress.currentPhase !== "research") errors.push("running stage should be research");
  if (progress.items.some((item) => item.phase === "strategy" && item.tone !== "done")) {
    errors.push("strategy should be complete after supervisor decision");
  }

  const paused = computePhaseProgress([...runningEvents, event("hitl_interrupt")], { paused: true });
  if (paused.percent !== progress.percent) {
    errors.push("HITL pause must not change progress");
  }
  return errors;
}

function checkFastPathTerminal(): string[] {
  const errors: string[] = [];
  const events = [
    event("brief", { status: "ok" }),
    event("topology", { eligible: true }),
    event("worker", { status: "start" }),
    event("worker", { status: "succeeded", execution_status: "succeeded" }),
    event("coverage", { status: "sufficient" }),
    event("task_result", { status: "completed" })
  ];
  const progress = computePhaseProgress(events);
  if (progress.percent !== 100) errors.push("fast path terminal should reach 100%");
  if (progress.stageOrder.includes("strategy")) errors.push("fast path must not require supervisor stage");
  if (progress.terminalStatus !== "completed") errors.push("fast path terminal status mismatch");
  return errors;
}

function checkPartialTerminal(): string[] {
  const events = [
    event("brief", { status: "ok" }),
    event("worker", { status: "failed", execution_status: "failed" }),
    event("termination", { status: "partial", outcome: "partial" })
  ];
  const progress = computePhaseProgress(events);
  const status = deriveRunStatus({ isRunning: false, events, result: "partial answer" });
  const errors: string[] = [];
  if (progress.terminalStatus !== "partial") errors.push("partial terminal projection mismatch");
  if (progress.percent !== 100) errors.push("partial terminal should be terminal-complete in progress");
  if (status !== "partial") errors.push("partial run must not be rendered as completed");
  return errors;
}

function checkMultiWaveMonotonic(): string[] {
  const waveOne = [
    event("brief", { status: "ok" }),
    event("topology", { eligible: false }),
    event("supervisor", { status: "start" }),
    event("supervisor", { status: "decided" }),
    event("worker", { status: "start", task_id: "w1" }),
    event("worker", { status: "start", task_id: "w2" }),
    event("worker", { status: "succeeded", task_id: "w1" }),
    event("worker", { status: "succeeded", task_id: "w2" }),
    event("coverage", { status: "gap" })
  ];
  const waveTwo = [
    ...waveOne,
    event("supervisor", { status: "start" }),
    event("supervisor", { status: "decided" }),
    event("worker", { status: "start", task_id: "w3" }),
    event("worker", { status: "start", task_id: "w4" }),
    event("worker", { status: "succeeded", task_id: "w3" })
  ];
  const before = computePhaseProgress(waveOne).percent;
  const after = computePhaseProgress(waveTwo).percent;
  return after < before ? ["multi-wave progress must be monotonic"] : [];
}

function checkSddPhaseProjection(): string[] {
  const errors: string[] = [];
  const caseOne = [
    event("brief", { status: "ok", canonical_event: "brief.compiled" }),
    event("topology", { eligible: false, canonical_event: "topology.decided" }),
    event("supervisor", { status: "start", canonical_event: "supervisor.started" }),
    event("worker", { status: "start", task_id: "w1", canonical_event: "worker.started" })
  ];
  const caseOneStates = new Map(projectPhaseStates(caseOne).map((state) => [state.phase, state]));
  if (caseOneStates.get("understand")?.state !== "completed") {
    errors.push("brief.compiled must complete understand");
  }
  if (caseOneStates.get("strategy")?.state !== "completed") {
    errors.push("worker.started must complete strategy");
  }
  if (caseOneStates.get("research")?.state !== "running") {
    errors.push("started worker must keep research running");
  }

  const partialWorkers = [
    event("brief", { status: "ok", canonical_event: "brief.compiled" }),
    event("topology", { eligible: false, canonical_event: "topology.decided" }),
    event("supervisor", { status: "start", canonical_event: "supervisor.started" }),
    ...Array.from({ length: 7 }, (_, index) =>
      event("worker", { status: "start", task_id: `w${index}`, canonical_event: "worker.started" })
    ),
    ...Array.from({ length: 5 }, (_, index) =>
      event("worker", {
        status: "partial",
        execution_status: "partial",
        task_id: `w${index}`,
        canonical_event: "worker.completed"
      })
    ),
    ...Array.from({ length: 2 }, (_, index) =>
      event("worker", {
        status: "failed",
        execution_status: "failed",
        task_id: `w${index + 5}`,
        canonical_event: "worker.failed"
      })
    )
  ];
  const research = projectPhaseStates(partialWorkers).find((state) => state.phase === "research");
  if (research?.state !== "partial" || research.detail !== "7 tasks · 5 partial · 2 failed") {
    errors.push("research must aggregate all worker outcomes");
  }

  const gap = projectPhaseStates([
    event("brief", { status: "ok", canonical_event: "brief.compiled" }),
    event("coverage", { status: "gap", canonical_event: "coverage.assessed" })
  ]).find((state) => state.phase === "coverage");
  if (gap?.state !== "insufficient" || formatPhaseStatus("coverage", gap.state) !== "不足") {
    errors.push("coverage gap must render as insufficient");
  }

  const quality = projectPhaseStates([
    event("quality", { status: "QUALITY_REJECTED", canonical_event: "quality.assessed" })
  ]).find((state) => state.phase === "quality");
  if (quality?.state !== "failed" || formatPhaseStatus("quality", quality.state) !== "未通过") {
    errors.push("quality rejection must render as failed");
  }

  const terminalPartial = computePhaseProgress([
    ...partialWorkers,
    event("coverage", { status: "gap", canonical_event: "coverage.assessed" }),
    event("synthesis", { status: "failed", canonical_event: "synthesis.failed" }),
    event("quality", { status: "fail", canonical_event: "quality.assessed" }),
    event("termination", { status: "partial", outcome: "partial", canonical_event: "run.terminated" })
  ]);
  if (terminalPartial.percent !== 100 || terminalPartial.terminalStatus !== "partial") {
    errors.push("terminal partial must be lifecycle-complete without claiming success");
  }
  if (terminalPartial.currentLabel !== "流程已结束") {
    errors.push("terminal partial label must mean lifecycle end");
  }
  return errors;
}

function checkStatsAndRunIsolation(): string[] {
  const events = [
    event("tool_start", { tool_name: "internet_search" }, "run-current"),
    event("tool_end", { tool_name: "internet_search" }, "run-current"),
    event("tool_start", { tool_name: "fetch_url" }, "run-current"),
    event("tool_error", { tool_name: "fetch_url" }, "run-current"),
    event("worker", { status: "start", task_id: "w1" }, "run-current"),
    event("worker", {
      status: "stopped",
      execution_status: "stopped",
      result_status: "partial",
      task_id: "w1"
    }, "run-current"),
    event("worker", { status: "start", task_id: "w2" }, "run-old"),
    event("worker", { status: "failed", task_id: "w2" }, "run-old")
  ];
  const stats = deriveRunStats(events, { fileCount: 2, runId: "run-current" });
  const errors: string[] = [];
  if (stats.toolCalls !== 2) errors.push("tool calls must count tool.started only");
  if (stats.toolFailed !== 1) errors.push("tool failures must count tool.failed only");
  if (stats.workerRuns !== 1) errors.push("worker runs must be run-scoped");
  if (stats.workerPartial !== 1) errors.push("partial worker outcome mismatch");
  if (stats.workerFailed !== 0) errors.push("old-run worker failure must be ignored");
  if (stats.fileCount !== 2) errors.push("file count mismatch");
  if (eventBelongsToRun(events[6], "run-current")) errors.push("old run event must be ignored");
  if (!eventBelongsToRun(events[0], "run-current")) errors.push("current run event must be accepted");
  return errors;
}

function checkRunSourceProjection(): string[] {
  const events = [
    event("tool_start", { tool_name: "internet_search", canonical_event: "tool.started" }),
    event("tool_start", { tool_name: "fetch_url", canonical_event: "tool.started" }),
    event("evidence", {
      canonical_event: "evidence.registered",
      evidence_id: "ev-1",
      source_id: "example.com",
      source_kind: "url",
      source_tier: "PRIMARY"
    }),
    event("evidence", {
      canonical_event: "evidence.registered",
      evidence_id: "ev-1",
      source_id: "example.com",
      source_kind: "url",
      source_tier: "PRIMARY"
    }),
    event("evidence", {
      canonical_event: "evidence.registered",
      evidence_id: "ev-2",
      source_kind: "file",
      source_tier: "SECONDARY"
    })
  ];
  const sources = deriveRunSources(events, { runId: "run-current" });
  const errors: string[] = [];
  if (sources.webSearchQueries !== 1) errors.push("source projection must count search tools only");
  if (sources.evidenceSources !== 2) errors.push("source projection must deduplicate evidence IDs");
  if (sources.webSources !== 1) errors.push("web source count mismatch");
  if (sources.uploadedFiles !== 1) errors.push("uploaded source count mismatch");
  if (sources.primarySources !== 1 || sources.otherSecondarySources !== 1) errors.push("source tier count mismatch");
  return errors;
}

export function runProgressSelfCheck(): string[] {
  const errors = [
    ...checkRunningProgress(),
    ...checkFastPathTerminal(),
    ...checkPartialTerminal(),
    ...checkMultiWaveMonotonic(),
    ...checkSddPhaseProjection(),
    ...checkStatsAndRunIsolation(),
    ...checkRunSourceProjection()
  ];
  errors.push(...elapsedClockSelfCheck().map((error) => `elapsed: ${error}`));
  return errors;
}
