import {
  BranchesOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  CloudServerOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  PauseCircleOutlined,
  StopOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { Button } from "antd";
import { useEffect, useRef, useState } from "react";
import { DeliverableFiles, linkifyArtifactNames } from "./DeliverableFiles";
import {
  elapsedClockIsTicking,
  formatElapsedClock,
  readElapsedMs,
  type ElapsedClockState
} from "../lib/elapsedClock";
import { computePhaseProgress } from "../lib/phaseProgress";
import { type RunStatus } from "../lib/runStatus";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { ResizeHandle } from "./ResizeHandle";
import { RunProgress } from "./RunProgress";
import type { MonitorMessage, OutputFile } from "../types";

export interface ChatTurn {
  id: string;
  content: string;
  events: MonitorMessage[];
  files: OutputFile[];
  isRunning: boolean;
  result: string;
  timestamp: string;
  elapsedClock: ElapsedClockState;
}

interface ConversationThreadProps {
  hasMoreEvents?: boolean;
  onLoadOlderEvents?: () => void;
  onProcessHeightChange?: (value: number) => void;
  onUseExample: (prompt: string) => void;
  processHeight?: number;
  runStatus?: RunStatus;
  sessionId?: string;
  turns: ChatTurn[];
}

const TASK_EXAMPLES = [
  {
    tool: "Harness",
    title: "多实体对照",
    prompt: "对比 LangGraph、Temporal 和 DeepSeek Harness 在 durable agent workflow 上的设计，要求引用一手资料。",
    icon: <CloudServerOutlined aria-hidden />,
  },
  {
    tool: "Harness",
    title: "机制分解",
    prompt: "研究 Agent 长任务里 Progress Evaluator 和 Replan 应该如何分工，列出证据缺口。",
    icon: <CloudServerOutlined aria-hidden />,
  },
  {
    tool: "Environment",
    title: "本地附件",
    prompt: "请读取我上传的文件，提炼核心主张、冲突点和仍缺的证据。",
    icon: <FileTextOutlined aria-hidden />,
  },
];

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "--:--";
  }
  return date.toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
}

function EventIcon({ event }: { event: string }) {
  if (event === "assistant_call") {
    return <BranchesOutlined aria-hidden />;
  }
  if (event === "tool_start") {
    return <ToolOutlined aria-hidden />;
  }
  if (event === "session_created") {
    return <FileSearchOutlined aria-hidden />;
  }
  if (event === "task_result") {
    return <CheckCircleOutlined aria-hidden />;
  }
  if (event === "task_cancelled") {
    return <StopOutlined aria-hidden />;
  }
  if (event === "error") {
    return <CloseCircleOutlined aria-hidden />;
  }
  if (event === "hitl_interrupt") {
    return <PauseCircleOutlined aria-hidden />;
  }
  return <ClockCircleOutlined aria-hidden />;
}

function ThinkingTimeline({
  events,
  maxHeight,
  hasMoreEvents,
  onLoadOlderEvents,
}: {
  events: MonitorMessage[];
  maxHeight?: number;
  hasMoreEvents?: boolean;
  onLoadOlderEvents?: () => void;
}) {
  const timelineRef = useRef<HTMLOListElement | null>(null);

  useEffect(() => {
    const timelineNode = timelineRef.current;
    if (!timelineNode) {
      return;
    }

    window.requestAnimationFrame(() => {
      timelineNode.scrollTop = timelineNode.scrollHeight;
    });
  }, [events.length]);

  if (events.length === 0) {
    return (
      <div className="thinking-empty">
        <ClockCircleOutlined aria-hidden />
        等待后端推送执行事件
      </div>
    );
  }

  return (
    <ol
      className="thinking-timeline"
      ref={timelineRef}
      style={maxHeight ? { maxHeight, minHeight: Math.min(120, maxHeight), height: maxHeight } : undefined}
    >
      {hasMoreEvents && onLoadOlderEvents ? (
        <li className="thinking-event">
          <Button size="small" type="link" onClick={onLoadOlderEvents}>
            加载更早事件
          </Button>
        </li>
      ) : null}
      {events.map((event, index) => (
        <li
          className={`thinking-event thinking-event--${event.event}`}
          key={event.event_id || `${event.run_id || "run"}:${event.seq ?? index}:${event.timestamp}`}
        >
          <span className="thinking-event-icon">
            <EventIcon event={event.event} />
          </span>
          <div>
            <div className="thinking-event-meta">
              <span>{event.event}</span>
              <time dateTime={event.timestamp}>
                {formatTime(event.timestamp)}
              </time>
            </div>
            <p>{event.message}</p>
            {event.event === "assistant_call" ||
            event.event === "tool_start" ? (
              <code>{JSON.stringify(event.data)}</code>
            ) : null}
          </div>
        </li>
      ))}
    </ol>
  );
}

function ProcessDock({
  elapsedClock,
  events,
  hasMoreEvents,
  isLatest,
  onLoadOlderEvents,
  onProcessHeightChange,
  processHeight,
  runStatus,
}: {
  elapsedClock: ElapsedClockState;
  events: MonitorMessage[];
  hasMoreEvents?: boolean;
  isLatest: boolean;
  onLoadOlderEvents?: () => void;
  onProcessHeightChange?: (value: number) => void;
  processHeight?: number;
  runStatus: RunStatus;
}) {
  const [open, setOpen] = useState(isLatest);
  const height = processHeight ?? 280;
  const phaseProgress = computePhaseProgress(events, {
    paused: runStatus === "awaiting_approval",
    completed: runStatus === "completed",
  });

  function handleResize(next: number) {
    if (!open) {
      setOpen(true);
    }
    onProcessHeightChange?.(next);
  }

  return (
    <section
      className={`process-dock process-dock--inline ${open ? "process-dock--open" : ""}`}
      aria-label="过程框"
    >
      <div className="process-dock-progress">
        <RunProgress
          durationLabel={<ElapsedTimer clock={elapsedClock} />}
          progress={phaseProgress}
          runStatus={runStatus}
        />
        <button
          aria-expanded={open}
          className="process-dock-toggle"
          onClick={() => setOpen((current) => !current)}
          type="button"
        >
          <BranchesOutlined aria-hidden />
          <span>{open ? "收起执行过程" : "展开执行过程"}</span>
          <strong>{events.length}</strong>
        </button>
      </div>
      {open ? (
        <div className="process-dock-stream">
          <ThinkingTimeline
            events={events}
            hasMoreEvents={isLatest ? hasMoreEvents : false}
            maxHeight={height}
            onLoadOlderEvents={isLatest ? onLoadOlderEvents : undefined}
          />
        </div>
      ) : null}
      {isLatest && onProcessHeightChange ? (
        <ResizeHandle
          axis="y"
          label="拖动调整过程框高度"
          max={720}
          min={120}
          onChange={handleResize}
          value={height}
        />
      ) : (
        <div className="process-dock-rail" aria-hidden />
      )}
    </section>
  );
}

function ElapsedTimer({ clock }: { clock: ElapsedClockState }) {
  const [now, setNow] = useState(() => Date.now());
  const ticking = elapsedClockIsTicking(clock);

  useEffect(() => {
    if (!ticking) {
      return;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [ticking]);

  return <>{formatElapsedClock(readElapsedMs(clock, now))}</>;
}

function AssistantMessage({
  elapsedClock,
  events,
  files,
  isRunning,
  result,
  runStatus,
  sessionId,
}: Pick<ChatTurn, "events" | "files" | "isRunning" | "result"> & {
  elapsedClock: ElapsedClockState;
  runStatus: RunStatus;
  sessionId?: string;
}) {
  const durationLabel = <ElapsedTimer clock={elapsedClock} />;
  const isCancelled = events.some((event) => event.event === "task_cancelled");
  const clockLive = runStatus === "running" || runStatus === "cancelling";
  const syncLabel =
    runStatus === "awaiting_approval"
      ? `已暂停 · ${durationLabel}`
      : clockLive
        ? `生成中 · 用时 ${durationLabel}`
        : `${isCancelled ? "已取消" : "已同步"} · 用时 ${durationLabel}`;

  return (
    <article className="chat-message chat-message--assistant">
      <div className="message-avatar">AI</div>
      <div className="message-bubble">
        <div className="message-meta">
          <span>Harness</span>
          <time>{syncLabel}</time>
        </div>

        {files.length > 0 ? (
          <div className="deliverable-banner" aria-label="可下载文件">
            <div className="result-sheet-kicker">FILES</div>
            <DeliverableFiles files={files} sessionId={sessionId} variant="banner" />
          </div>
        ) : null}

        {result ? (
          <div className="assistant-answer result-sheet">
            <div className="result-sheet-kicker">RESULT</div>
            <MarkdownRenderer content={linkifyArtifactNames(result, files, sessionId)} />
          </div>
        ) : (
          <div
            className={`assistant-answer assistant-answer--pending ${
              runStatus === "awaiting_approval" ? "assistant-answer--paused" : ""
            }`}
          >
            {isRunning || runStatus === "awaiting_approval" ? (
              <p className="assistant-pending-copy">提问下方会依次显示过程框、执行过程，完成后回复落在这里。</p>
            ) : (
              "任务完成后会在这里显示最终回复。"
            )}
          </div>
        )}

      </div>
    </article>
  );
}

function resultStatus(turn: ChatTurn): RunStatus {
  if (turn.events.some((event) => event.event === "error")) {
    return "failed";
  }
  if (turn.result || turn.events.some((event) => event.event === "task_result")) {
    return "completed";
  }
  if (turn.events.some((event) => event.event === "task_cancelled")) {
    return "idle";
  }
  return "idle";
}

export function ConversationThread({
  hasMoreEvents,
  onLoadOlderEvents,
  onProcessHeightChange,
  onUseExample,
  processHeight,
  runStatus = "idle",
  sessionId,
  turns,
}: ConversationThreadProps) {
  if (turns.length === 0) {
    return (
      <div className="conversation-empty">
        <div className="empty-examples">
          <div className="empty-examples-copy">
            <span className="panel-kicker">TASK EXAMPLES</span>
            <h3>发起一次长任务</h3>
            <p>
              每个任务走 Brief → Plan → Worker → Progress / Replan。
            </p>
          </div>

          <div className="example-grid" aria-label="研搜任务示例">
            {TASK_EXAMPLES.map((example) => (
              <button
                className="example-card"
                key={example.tool}
                onClick={() => onUseExample(example.prompt)}
                type="button"
              >
                <span className="example-icon">{example.icon}</span>
                <span className="example-copy">
                  <span>{example.tool}</span>
                  <strong>{example.title}</strong>
                  <small>{example.prompt}</small>
                </span>
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="conversation-thread" aria-label="聊天消息流">
      {turns.map((turn, index) => {
        const isLatest = index === turns.length - 1;
        const turnStatus = turn.isRunning ? runStatus : resultStatus(turn);
        const showProcess = turn.events.length > 0 || turn.isRunning || turnStatus !== "idle";

        return (
          <div className="conversation-turn" key={turn.id}>
            <article className="chat-message chat-message--user">
              <div className="message-bubble">
                <div className="message-meta">
                  <span>你</span>
                  <time dateTime={turn.timestamp}>
                    {formatTime(turn.timestamp)}
                  </time>
                </div>
                <p>{turn.content}</p>
              </div>
            </article>

            {showProcess ? (
              <ProcessDock
                elapsedClock={turn.elapsedClock}
                events={turn.events}
                hasMoreEvents={hasMoreEvents}
                isLatest={isLatest}
                onLoadOlderEvents={onLoadOlderEvents}
                onProcessHeightChange={onProcessHeightChange}
                processHeight={processHeight}
                runStatus={turnStatus}
              />
            ) : null}

            <AssistantMessage
              elapsedClock={turn.elapsedClock}
              events={turn.events}
              files={turn.files}
              isRunning={turn.isRunning}
              result={turn.result}
              runStatus={turnStatus}
              sessionId={sessionId}
            />
          </div>
        );
      })}
    </div>
  );
}
