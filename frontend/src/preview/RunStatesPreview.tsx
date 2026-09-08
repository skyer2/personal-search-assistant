import { RunProgress } from "../components/RunProgress";
import { computePhaseProgress } from "../lib/phaseProgress";
import { runProgressSelfCheck } from "../lib/runProgressSelfCheck";
import type { MonitorMessage } from "../types";

function event(
  name: MonitorMessage["event"],
  message: string,
  data: Record<string, unknown> = {},
  timestamp = "2026-08-28T06:40:00.000Z"
): MonitorMessage {
  return {
    type: "monitor_event",
    event: name,
    message,
    data,
    timestamp
  };
}

const runningEvents: MonitorMessage[] = [
  event("session_created", "工作目录已创建", { path: "/tmp/session" }),
  event("phase", "[understand] ✓ done", { phase: "understand", status: "done", duration_ms: 420 }),
  event("phase", "[plan] ✓ done", { phase: "plan", status: "done", duration_ms: 880 }),
  event("phase", "[execute] → start", {
    phase: "execute",
    status: "start",
    step_index: 1,
    total_steps: 3,
    step_type: "database_query"
  })
];

const pausedEvents: MonitorMessage[] = [
  ...runningEvents,
  event("hitl_interrupt", "等待人工审批（1 个动作）", {
    gate_type: "step",
    action_requests: [{ name: "database_query", args: { description: "查询库存" } }]
  })
];

export function RunStatesPreview() {
  const checkErrors = runProgressSelfCheck();
  const running = computePhaseProgress(runningEvents);
  const paused = computePhaseProgress(pausedEvents, { paused: true });
  const done = computePhaseProgress([
    ...runningEvents,
    event("phase", "[build_context] ✓ done", { phase: "build_context", status: "done" }),
    event("phase", "[execute] ✓ done", { phase: "execute", status: "done" }),
    event("phase", "[compress] ✓ done", { phase: "compress", status: "done" }),
    event("phase", "[validate] ✓ done", { phase: "validate", status: "done" }),
    event("phase", "[finalize] ✓ done", { phase: "finalize", status: "done" })
  ]);

  return (
    <div className="run-states-preview">
      <div>
        <span className="panel-kicker">DEV PREVIEW</span>
        <h1>任务进度状态对照</h1>
        <p>运行中会缓慢呼吸；审批暂停必须静止，不得扫光或闪烁。</p>
        {checkErrors.length > 0 ? (
          <p role="alert">进度自检失败：{checkErrors.join("；")}</p>
        ) : (
          <p>进度状态机自检通过。</p>
        )}
      </div>

      <section className="run-states-card">
        <h2>运行中</h2>
        <RunProgress durationLabel="00:18" progress={running} runStatus="running" />
      </section>

      <section className="run-states-card">
        <h2>人工审批暂停</h2>
        <RunProgress durationLabel="00:18" progress={paused} runStatus="awaiting_approval" />
      </section>

      <section className="run-states-card">
        <h2>已完成</h2>
        <RunProgress durationLabel="01:12" progress={done} runStatus="completed" />
      </section>
    </div>
  );
}
