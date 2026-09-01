import {
  BranchesOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  DownloadOutlined,
  FileMarkdownOutlined,
  FilePdfOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  PauseCircleOutlined,
  StopOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { Button, Tooltip } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { getDownloadUrl } from "../lib/api";
import { computePhaseProgress } from "../lib/phaseProgress";
import { type RunStatus } from "../lib/runStatus";
import { MarkdownRenderer } from "./MarkdownRenderer";
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
}

interface ConversationThreadProps {
  onUseExample: (prompt: string) => void;
  runStatus?: RunStatus;
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

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function parseTime(value: string): number | null {
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? null : time;
}

function formatDuration(value: number): string {
  const totalSeconds = Math.max(0, Math.floor(value / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const paddedMinutes = String(minutes).padStart(2, "0");
  const paddedSeconds = String(seconds).padStart(2, "0");

  if (hours > 0) {
    return `${hours}:${paddedMinutes}:${paddedSeconds}`;
  }
  return `${paddedMinutes}:${paddedSeconds}`;
}

function getLastEventTime(
  events: MonitorMessage[],
  eventName?: string,
): number | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (!eventName || event.event === eventName) {
      return parseTime(event.timestamp);
    }
  }
  return null;
}

function getThinkingDuration(
  events: MonitorMessage[],
  fallbackStart: string,
  freezeClock: boolean,
  now: number,
): string {
  const startedAt =
    (events[0] ? parseTime(events[0].timestamp) : null) ??
    parseTime(fallbackStart) ??
    now;
  const finishedAt =
    getLastEventTime(events, "task_result") ??
    (freezeClock
      ? getLastEventTime(events, "hitl_interrupt") ?? getLastEventTime(events)
      : null) ??
    now;
  return formatDuration(finishedAt - startedAt);
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

function FileIcon({ name }: { name: string }) {
  if (name.endsWith(".pdf")) {
    return <FilePdfOutlined aria-hidden />;
  }
  if (name.endsWith(".md")) {
    return <FileMarkdownOutlined aria-hidden />;
  }
  return <FileTextOutlined aria-hidden />;
}

function ThinkingTimeline({ events }: { events: MonitorMessage[] }) {
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
    <ol className="thinking-timeline" ref={timelineRef}>
      {events.map((event, index) => (
        <li
          className={`thinking-event thinking-event--${event.event}`}
          key={`${event.timestamp}-${index}`}
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

function ArtifactShelf({ files }: { files: OutputFile[] }) {
  if (files.length === 0) {
    return (
      <div className="artifact-empty">
        <FileSearchOutlined aria-hidden />
        暂无输出文件
      </div>
    );
  }

  return (
    <div className="artifact-shelf">
      {files.map((file) => (
        <div className="artifact-card" key={file.path}>
          <span className="artifact-icon">
            <FileIcon name={file.name} />
          </span>
          <div className="artifact-copy">
            <strong title={file.name}>{file.name}</strong>
            <span>{formatBytes(file.size)}</span>
          </div>
          <Tooltip title="下载">
            <Button
              aria-label={`下载 ${file.name}`}
              className="artifact-download"
              href={getDownloadUrl(file.path)}
              icon={<DownloadOutlined />}
              shape="circle"
            />
          </Tooltip>
        </div>
      ))}
    </div>
  );
}

function AssistantMessage({
  events,
  files,
  isRunning,
  result,
  runStatus,
  timestamp,
}: Pick<ChatTurn, "events" | "files" | "isRunning" | "result" | "timestamp"> & {
  runStatus: RunStatus;
}) {
  const [now, setNow] = useState(Date.now());
  const clockLive = runStatus === "running" || runStatus === "cancelling";

  useEffect(() => {
    if (!clockLive) {
      return;
    }

    const timer = window.setInterval(() => {
      setNow(Date.now());
    }, 1000);

    return () => window.clearInterval(timer);
  }, [clockLive]);

  const freezeClock = !clockLive;
  const durationLabel = getThinkingDuration(events, timestamp, freezeClock, now);
  const isCancelled = events.some((event) => event.event === "task_cancelled");
  const progress = useMemo(
    () =>
      computePhaseProgress(events, {
        paused: runStatus === "awaiting_approval",
        completed: runStatus === "completed"
      }),
    [events, runStatus]
  );
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
          <span>DeepSearch Agents</span>
          <time>{syncLabel}</time>
        </div>

        <details
          className="thinking-block"
          open={isRunning || events.length > 0}
        >
          <summary>
            <span>
              <BranchesOutlined aria-hidden />
              深度研搜过程
            </span>
            <strong>{events.length}</strong>
          </summary>
          <ThinkingTimeline events={events} />
        </details>

        {result ? (
          <div className="assistant-answer">
            <MarkdownRenderer content={result} />
          </div>
        ) : (
          <div
            className={`assistant-answer assistant-answer--pending ${
              runStatus === "awaiting_approval" ? "assistant-answer--paused" : ""
            }`}
          >
            {isRunning || runStatus === "awaiting_approval" ? (
              <RunProgress
                durationLabel={durationLabel}
                progress={progress}
                runStatus={runStatus}
              />
            ) : (
              "任务完成后会在这里显示最终回复。"
            )}
          </div>
        )}

        <details
          className="thinking-block artifact-block"
          open={files.length > 0}
        >
          <summary>
            <span>
              <FileSearchOutlined aria-hidden />
              输出文件
            </span>
            <strong>{files.length}</strong>
          </summary>
          <ArtifactShelf files={files} />
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
  return "idle";
}

export function ConversationThread({
  onUseExample,
  runStatus = "idle",
  turns,
}: ConversationThreadProps) {
  if (turns.length === 0) {
    return (
      <div className="conversation-empty">
        <div className="empty-examples">
          <div className="empty-examples-copy">
            <span className="panel-kicker">TASK EXAMPLES</span>
            <h3>用研究任务驱动 Harness</h3>
            <p>
              这不是搜索产品。每个任务走 Brief → Plan → Worker → Progress / Replan。
              Search 只是 environment tool。
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
      {turns.map((turn) => (
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
          <AssistantMessage
            events={turn.events}
            files={turn.files}
            isRunning={turn.isRunning}
            result={turn.result}
            runStatus={turn.isRunning ? runStatus : resultStatus(turn)}
            timestamp={turn.timestamp}
          />
        </div>
      ))}
    </div>
  );
}
