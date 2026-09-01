import {
  BranchesOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  FileSearchOutlined,
  FlagOutlined,
  NodeIndexOutlined,
  OrderedListOutlined,
  PauseCircleOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  ScissorOutlined,
  ThunderboltOutlined,
  ToolOutlined
} from "@ant-design/icons";
import { Empty, Tag } from "antd";
import { PHASE_LABELS, buildPhaseTimeline, type PhaseTone } from "../lib/phaseProgress";
import type { RunStatus } from "../lib/runStatus";
import type { MonitorMessage } from "../types";

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "--:--:--";
  }
  return date.toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

function EventIcon({ event, phase }: { event: string; phase?: string }) {
  if (event === "hitl_interrupt") {
    return <PauseCircleOutlined aria-hidden />;
  }
  if (event === "phase") {
    if (phase === "understand") return <NodeIndexOutlined aria-hidden />;
    if (phase === "plan") return <OrderedListOutlined aria-hidden />;
    if (phase === "execute") return <ThunderboltOutlined aria-hidden />;
    if (phase === "compress") return <ScissorOutlined aria-hidden />;
    if (phase === "validate") return <SafetyCertificateOutlined aria-hidden />;
    if (phase === "recover") return <ReloadOutlined aria-hidden />;
    if (phase === "finalize") return <FlagOutlined aria-hidden />;
    return <ClockCircleOutlined aria-hidden />;
  }
  if (event === "assistant_call") {
    return <BranchesOutlined aria-hidden />;
  }
  if (event === "tool_start") {
    return <ToolOutlined aria-hidden />;
  }
  if (event === "tool_end") {
    return <CheckCircleOutlined aria-hidden />;
  }
  if (event === "tool_error") {
    return <CloseCircleOutlined aria-hidden />;
  }
  if (event === "worker" || event === "replan" || event === "progress") {
    return <BranchesOutlined aria-hidden />;
  }
  if (event === "session_created") {
    return <FileSearchOutlined aria-hidden />;
  }
  if (event === "task_result") {
    return <CheckCircleOutlined aria-hidden />;
  }
  if (event === "error") {
    return <CloseCircleOutlined aria-hidden />;
  }
  return <ClockCircleOutlined aria-hidden />;
}

function toneTag(tone: PhaseTone): { color: string; label: string } {
  if (tone === "failed") return { color: "error", label: "失败" };
  if (tone === "done") return { color: "success", label: "完成" };
  if (tone === "paused") return { color: "warning", label: "已暂停" };
  if (tone === "running") return { color: "blue", label: "进行中" };
  return { color: "default", label: "待开始" };
}

function HarnessPhaseTimeline({
  events,
  paused
}: {
  events: MonitorMessage[];
  paused: boolean;
}) {
  const items = buildPhaseTimeline(events, { paused });
  if (items.length === 0) {
    return null;
  }

  return (
    <div className="harness-phase-timeline" aria-label="Harness 阶段时间线">
      <div className="harness-phase-heading">
        <span className="panel-kicker">HARNESS LOOP</span>
        <strong>阶段时间线</strong>
      </div>
      <ol className="phase-step-list">
        {items.map((item) => {
          const tag = toneTag(item.tone);
          return (
            <li className={`phase-step phase-step--${item.tone}`} key={item.phase}>
              <div className="phase-step-icon">
                {item.tone === "paused" ? (
                  <PauseCircleOutlined aria-hidden />
                ) : (
                  <EventIcon event="phase" phase={item.phase} />
                )}
              </div>
              <div className="phase-step-body">
                <div className="phase-step-title">
                  <span>{PHASE_LABELS[item.phase] ?? item.phase}</span>
                  <Tag color={tag.color}>{tag.label}</Tag>
                </div>
                <div className="phase-step-meta">
                  <time dateTime={item.timestamp}>{formatTime(item.timestamp)}</time>
                  {item.durationMs !== undefined ? <span>{item.durationMs}ms</span> : null}
                  {item.stepHint ? <span>{item.stepHint}</span> : null}
                </div>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

interface EventStreamProps {
  compact?: boolean;
  events: MonitorMessage[];
  runStatus?: RunStatus;
}

export function EventStream({ compact = false, events, runStatus = "idle" }: EventStreamProps) {
  const paused = runStatus === "awaiting_approval";

  return (
    <section
      className={`console-panel event-panel ${compact ? "event-panel--compact" : ""}`}
      aria-labelledby="event-title"
    >
      <div className="panel-heading">
        <div>
          <span className="panel-kicker">{paused ? "PAUSED" : "LIVE TRACE"}</span>
          <h2 id="event-title">执行轨迹</h2>
        </div>
        <span className="event-count">{events.length}</span>
      </div>

      <HarnessPhaseTimeline events={events} paused={paused} />

      {events.length === 0 ? (
        <div className="empty-console">
          <Empty description="等待 WebSocket 推送任务事件" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        </div>
      ) : (
        <details className="event-log-details">
          <summary>原始事件日志</summary>
          <ol className="event-stream">
            {events.map((event, index) => {
              const phase = event.event === "phase" ? String(event.data.phase ?? "") : "";
              const status = event.event === "phase" ? String(event.data.status ?? "") : "";
              const rowClass = [
                "event-row",
                `event-row--${event.event}`,
                phase ? `event-row--phase-${phase}` : "",
                status === "failed" ? "event-row--phase-failed" : ""
              ]
                .filter(Boolean)
                .join(" ");

              return (
                <li className={rowClass} key={`${event.timestamp}-${index}`}>
                  <div className="event-icon">
                    <EventIcon event={event.event} phase={phase} />
                  </div>
                  <div className="event-body">
                    <div className="event-meta">
                      <span>{event.event === "phase" ? `phase:${phase}` : event.event}</span>
                      <time dateTime={event.timestamp}>{formatTime(event.timestamp)}</time>
                    </div>
                    <p>{event.message}</p>
                  </div>
                </li>
              );
            })}
          </ol>
        </details>
      )}
    </section>
  );
}
