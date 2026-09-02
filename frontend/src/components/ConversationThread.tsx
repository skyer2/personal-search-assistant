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
import { formatElapsedClock } from "../lib/elapsedClock";
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
  elapsedMs: number;
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
      style={maxHeight ? { maxHeight, minHeight: Math.min(120, maxHeight) } : undefined}
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

function AssistantMessage({
  elapsedMs,
  events,
  files,
  hasMoreEvents,
  isLatest,
  isRunning,
  onLoadOlderEvents,
  onProcessHeightChange,
  processHeight,
  result,
  runStatus,
  sessionId,
}: Pick<ChatTurn, "events" | "files" | "isRunning" | "result"> & {
  elapsedMs: number;
  hasMoreEvents?: boolean;
  isLatest: boolean;
  onLoadOlderEvents?: () => void;
  onProcessHeightChange?: (value: number) => void;
  processHeight?: number;
  runStatus: RunStatus;
  sessionId?: string;
}) {
  const [processOpen, setProcessOpen] = useState(isLatest);
  const durationLabel = formatElapsedClock(elapsedMs);
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

        <details
          className="thinking-block"
          open={processOpen}
          onToggle={(event) => setProcessOpen(event.currentTarget.open)}
        >
          <summary>
            <span>
              <BranchesOutlined aria-hidden />
              执行过程
            </span>
            <strong>{events.length}</strong>
          </summary>
          <ThinkingTimeline
            events={events}
            hasMoreEvents={isLatest ? hasMoreEvents : false}
            maxHeight={isLatest ? processHeight : undefined}
            onLoadOlderEvents={isLatest ? onLoadOlderEvents : undefined}
          />
          {isLatest && processHeight && onProcessHeightChange ? (
            <ResizeHandle
              axis="y"
              label="拖动调整执行过程高度"
              max={720}
              min={120}
              onChange={onProcessHeightChange}
              value={processHeight}
            />
          ) : null}
        </details>

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

        <details className="thinking-block artifact-block" open={files.length > 0}>
          <summary>
            <span>
              <FileSearchOutlined aria-hidden />
              证据与相关文档
            </span>
            <strong>{files.length}</strong>
          </summary>
          <DeliverableFiles files={files} sessionId={sessionId} />
        </details>
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
        const phaseProgress = computePhaseProgress(turn.events, {
          paused: turnStatus === "awaiting_approval",
          completed: turnStatus === "completed",
        });

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
              <section className="process-dock process-dock--inline" aria-label="过程框">
                <div className="process-dock-progress">
                  <RunProgress
                    durationLabel={formatElapsedClock(turn.elapsedMs)}
                    progress={phaseProgress}
                    runStatus={turnStatus}
                  />
                </div>
              </section>
            ) : null}

            <AssistantMessage
              elapsedMs={turn.elapsedMs}
              events={turn.events}
              files={turn.files}
              hasMoreEvents={hasMoreEvents}
              isLatest={isLatest}
              isRunning={turn.isRunning}
              onLoadOlderEvents={onLoadOlderEvents}
              onProcessHeightChange={onProcessHeightChange}
              processHeight={processHeight}
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
