import { useEffect, useState } from "react";
import { ConversationThread, type ChatTurn } from "../components/ConversationThread";
import { formatElapsedClock, readElapsedMs, startElapsedClock } from "../lib/elapsedClock";
import { runProgressSelfCheck } from "../lib/runProgressSelfCheck";
import type { MonitorMessage, OutputFile } from "../types";

function event(
  name: MonitorMessage["event"],
  message: string,
  data: Record<string, unknown> = {},
  timestamp = "2026-09-01T12:00:08.000Z"
): MonitorMessage {
  return {
    type: "monitor_event",
    event: name,
    message,
    data,
    timestamp
  };
}

const mockEvents: MonitorMessage[] = [
  event("session_created", "工作目录已创建", { path: "/tmp/session" }, "2026-09-01T12:00:01.000Z"),
  event("phase", "[understand] ✓ done", { phase: "understand", status: "done", duration_ms: 420 }),
  event("phase", "[plan] ✓ done", { phase: "plan", status: "done", duration_ms: 880 }),
  event("assistant_call", "开始执行研究步骤", { step_index: 0 }),
  event("tool_start", "internet_search", { tool: "internet_search" })
];

const mockFiles: OutputFile[] = [
  { name: "brief.pdf", type: "file", path: "brief.pdf", size: 2048, mtime: Date.now() },
  { name: "brief.md", type: "file", path: "brief.md", size: 2048, mtime: Date.now() },
  { name: "working_notes.md", type: "file", path: "working_notes.md", size: 512, mtime: Date.now() }
];

export function ConversationLayoutPreview() {
  const [started] = useState(() => startElapsedClock(Date.now()));
  const [now, setNow] = useState(Date.now());
  const checkErrors = runProgressSelfCheck();

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, []);

  const elapsedMs = readElapsedMs(started, now);
  const turns: ChatTurn[] = [
    {
      id: "preview-turn",
      content: "对比 LangGraph 和 Temporal 在 durable agent workflow 上的设计。",
      events: mockEvents,
      files: mockFiles,
      isRunning: false,
      result:
        "任务因 deadline_exceeded 提前结束，已根据已有材料生成部分 PDF：brief.pdf",
      timestamp: new Date(now - elapsedMs).toISOString(),
      elapsedMs
    }
  ];

  return (
    <div className="conversation-layout-preview">
      <div>
        <span className="panel-kicker">DEV PREVIEW</span>
        <h1>提问后的排布与用时</h1>
        <p>顺序必须是：提问 → 过程框 → 执行过程 → 证据与相关文档。已用时应随墙钟增长。</p>
        {checkErrors.length > 0 ? (
          <p role="alert">自检失败：{checkErrors.join("；")}</p>
        ) : (
          <p>布局与计时自检通过。当前已用时 {formatElapsedClock(elapsedMs)}</p>
        )}
      </div>
      <ConversationThread
        onUseExample={() => undefined}
        processHeight={220}
        runStatus="completed"
        sessionId="preview-session"
        turns={turns}
      />
    </div>
  );
}
