import {
  ApiOutlined,
  BarChartOutlined,
  BranchesOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloudServerOutlined,
  FileTextOutlined,
  LineChartOutlined,
  MessageOutlined,
  PauseCircleOutlined,
  ToolOutlined
} from "@ant-design/icons";
import { Alert, App as AntApp, Button } from "antd";
import { useEffect, useRef, useState } from "react";
import { ApprovalPanel } from "./components/ApprovalPanel";
import { ChatComposer } from "./components/ChatComposer";
import { ConversationThread } from "./components/ConversationThread";
import type { ChatTurn } from "./components/ConversationThread";
import { EvalPanel } from "./components/EvalPanel";
import { EventStream } from "./components/EventStream";
import { TraceViewer } from "./components/TraceViewer";
import { API_BASE_URL, WS_BASE_URL } from "./lib/config";
import { useDeepAgentSession } from "./hooks/useDeepAgentSession";
import { isLiveRun, runStatusLabel } from "./lib/runStatus";
import type { ConnectionState, UploadedItem, WorkspaceTab } from "./types";

function connectionLabel(state: ConnectionState): string {
  const labels: Record<ConnectionState, string> = {
    connecting: "连接中",
    connected: "已连接",
    reconnecting: "重连中",
    closed: "已关闭"
  };
  return labels[state];
}

function createTurn(content: string): ChatTurn {
  return {
    id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}`,
    content,
    events: [],
    files: [],
    isRunning: true,
    result: "",
    timestamp: new Date().toISOString()
  };
}

export default function App() {
  const { message } = AntApp.useApp();
  const [query, setQuery] = useState("");
  const [stagedItems, setStagedItems] = useState<UploadedItem[]>([]);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [workspace, setWorkspace] = useState<WorkspaceTab>("chat");
  const streamRef = useRef<HTMLElement | null>(null);
  const failureHandledRef = useRef<string | null>(null);
  const session = useDeepAgentSession();

  useEffect(() => {
    setTurns((previous) => {
      if (previous.length === 0) {
        return previous;
      }

      const latestTurn = previous[previous.length - 1];
      const nextLatestTurn = {
        ...latestTurn,
        events: session.events,
        files: session.files,
        isRunning: session.isRunning,
        result: session.result
      };

      return [...previous.slice(0, -1), nextLatestTurn];
    });
  }, [session.events, session.files, session.isRunning, session.result]);

  useEffect(() => {
    if (!session.taskFailure) {
      return;
    }

    const failureKey = `${session.taskFailure.message}:${session.events.length}`;
    if (failureHandledRef.current === failureKey) {
      return;
    }
    failureHandledRef.current = failureKey;

    const failureMessage = session.taskFailure.message;
    setTurns((previous) => {
      if (previous.length === 0) {
        return previous;
      }
      const lastTurn = previous[previous.length - 1];
      setQuery(lastTurn.content);
      return previous.slice(0, -1);
    });
    session.discardFailedTask();
    message.warning(`任务失败，已撤回本次提问：${failureMessage}`);
  }, [message, session.discardFailedTask, session.taskFailure, session.events.length]);

  useEffect(() => {
    const streamNode = streamRef.current;
    if (!streamNode || workspace !== "chat") {
      return;
    }

    window.requestAnimationFrame(() => {
      streamNode.scrollTo({
        top: streamNode.scrollHeight,
        behavior: "smooth"
      });
    });
  }, [turns, workspace]);

  async function handleSubmit() {
    const cleanQuery = query.trim();
    if (!cleanQuery) {
      message.warning("请输入研搜任务");
      return;
    }

    const nextTurn = createTurn(cleanQuery);
    setTurns((previous) => [...previous, nextTurn]);
    setQuery("");
    setWorkspace("chat");

    try {
      await session.submitTask(cleanQuery, "agent");
      message.success("任务已启动，执行过程会显示在对话中");
    } catch (error) {
      setTurns((previous) => previous.slice(0, -1));
      setQuery(cleanQuery);
      message.error(error instanceof Error ? error.message : "任务启动失败");
    }
  }

  async function handleCancel() {
    try {
      const response = await session.cancelCurrentTask();
      message.info(response.status === "cancelling" ? "取消请求已发送，正在等待当前调用结束" : "任务已取消");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "取消任务失败");
    }
  }

  async function handleUpload(items: UploadedItem[]) {
    try {
      const response = await session.uploadFiles(items);
      setStagedItems([]);
      message.success(`已上传 ${response.files.length} 个文件`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "上传失败");
    }
  }

  function handleNewSession() {
    failureHandledRef.current = null;
    session.resetSession();
    setTurns([]);
    setQuery("");
    setStagedItems([]);
    setWorkspace("chat");
  }

  async function handleHitlDecisions(
    decisions: Array<{ type: "approve" | "reject" | "edit"; edited_action?: Record<string, unknown> }>
  ) {
    try {
      await session.submitHitlDecisions(decisions);
      message.success("审批已提交，任务继续执行");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "审批提交失败");
    }
  }

  const online = session.connectionState === "connected";

  return (
    <div className="chat-app-shell min-h-dvh">
      <aside className="chat-sidebar" aria-label="会话信息">
        <div className="sidebar-brand">
          <span className="panel-kicker">HARNESS</span>
          <h1>Research Agent</h1>
          <p>Long-running agent harness · 不是搜索引擎</p>
        </div>

        <Button className="new-chat-button" block onClick={handleNewSession}>
          新建任务
        </Button>

        <div className="workspace-nav">
          <Button
            className={workspace === "chat" ? "workspace-nav-btn workspace-nav-btn--active" : "workspace-nav-btn"}
            icon={<MessageOutlined aria-hidden />}
            onClick={() => setWorkspace("chat")}
            type={workspace === "chat" ? "primary" : "default"}
            block
          >
            对话
          </Button>
          <Button
            className={workspace === "eval" ? "workspace-nav-btn workspace-nav-btn--active" : "workspace-nav-btn"}
            icon={<BarChartOutlined aria-hidden />}
            onClick={() => setWorkspace("eval")}
            type={workspace === "eval" ? "primary" : "default"}
            block
          >
            Eval 面板
          </Button>
          <Button
            className={workspace === "trace" ? "workspace-nav-btn workspace-nav-btn--active" : "workspace-nav-btn"}
            icon={<LineChartOutlined aria-hidden />}
            onClick={() => setWorkspace("trace")}
            type={workspace === "trace" ? "primary" : "default"}
            block
          >
            Trace 查看器
          </Button>
        </div>

        <div className="sidebar-section">
          <span className="sidebar-label">THREAD</span>
          <strong className="thread-id" title={session.threadId}>
            {session.threadId.slice(0, 8)}
          </strong>
        </div>

        <div className="sidebar-status-list">
          <div className={`sidebar-status ${online ? "sidebar-status--online" : "sidebar-status--warn"}`}>
            <ApiOutlined aria-hidden />
            <span>WebSocket</span>
            <strong>{connectionLabel(session.connectionState)}</strong>
          </div>
          <div className={`sidebar-status sidebar-status--${session.runStatus}`}>
            {session.runStatus === "awaiting_approval" ? (
              <PauseCircleOutlined aria-hidden />
            ) : session.runStatus === "failed" ? (
              <CloseCircleOutlined aria-hidden />
            ) : (
              <BranchesOutlined aria-hidden />
            )}
            <span>任务态</span>
            <strong>{runStatusLabel(session.runStatus)}</strong>
          </div>
          <div className="sidebar-status">
            <ToolOutlined aria-hidden />
            <span>工具调用</span>
            <strong>{session.stats.toolEvents}</strong>
          </div>
          <div className="sidebar-status">
            <BranchesOutlined aria-hidden />
            <span>助手调度</span>
            <strong>{session.stats.assistantEvents}</strong>
          </div>
          <div className={session.stats.errorEvents > 0 ? "sidebar-status sidebar-status--error" : "sidebar-status"}>
            <CloseCircleOutlined aria-hidden />
            <span>异常</span>
            <strong>{session.stats.errorEvents}</strong>
          </div>
        </div>

        <div className="sidebar-section">
          <span className="sidebar-label">SOURCES</span>
          <ul className="agent-mini-list">
            <li>
              <CloudServerOutlined aria-hidden />
              环境：web_search
            </li>
            <li>
              <FileTextOutlined aria-hidden />
              上传文件 / URL
            </li>
          </ul>
        </div>

        <div className="sidebar-section sidebar-endpoints">
          <span className="sidebar-label">ENDPOINTS</span>
          <code>{API_BASE_URL || `${window.location.origin} (dev proxy)`}</code>
          <code>{WS_BASE_URL}</code>
        </div>
      </aside>

      <main className="chat-main">
        <header className="chat-topbar">
          <div>
            <span className="panel-kicker">
              {workspace === "chat" ? "CHAT WORKSPACE" : workspace === "eval" ? "EVAL PANEL" : "TRACE VIEWER"}
            </span>
            <h2>
              {workspace === "chat"
                ? "Harness 运行"
                : workspace === "eval"
                  ? "Developer · Eval"
                  : "Developer · Trace"}
            </h2>
          </div>
          <div
            className={`run-indicator run-indicator--${session.runStatus}`}
            aria-live="polite"
          >
            {session.runStatus === "awaiting_approval" ? (
              <>
                <PauseCircleOutlined aria-hidden /> {runStatusLabel(session.runStatus)}
              </>
            ) : isLiveRun(session.runStatus) ? (
              <>
                <BranchesOutlined aria-hidden /> {runStatusLabel(session.runStatus)}
              </>
            ) : (
              <>
                <CheckCircleOutlined aria-hidden /> {runStatusLabel(session.runStatus)}
              </>
            )}
          </div>
        </header>

        {session.lastError ? (
          <Alert className="chat-alert" message={session.lastError} showIcon type="error" />
        ) : null}

        {session.hitlPending ? (
          <ApprovalPanel
            isSubmitting={session.isHitlSubmitting}
            onApproveAll={() =>
              void handleHitlDecisions(
                session.hitlPending!.action_requests.map(() => ({ type: "approve" as const }))
              )
            }
            onDecide={(decisions) => void handleHitlDecisions(decisions)}
            onRejectAll={() =>
              void handleHitlDecisions(
                session.hitlPending!.action_requests.map(() => ({ type: "reject" as const }))
              )
            }
            payload={session.hitlPending}
          />
        ) : null}

        {workspace === "chat" ? (
          <section className="chat-stream-panel" ref={streamRef}>
            <EventStream events={session.events} runStatus={session.runStatus} />
            <ConversationThread
              onUseExample={setQuery}
              runStatus={session.runStatus}
              turns={turns}
            />
          </section>
        ) : null}

        {workspace === "eval" ? <EvalPanel /> : null}
        {workspace === "trace" ? <TraceViewer sessionId={session.threadId} /> : null}

        {workspace === "chat" ? (
          <ChatComposer
            isAwaitingApproval={session.runStatus === "awaiting_approval"}
            isCancelling={session.isCancelling}
            isRunning={session.isRunning}
            isUploading={session.isUploading}
            onCancel={handleCancel}
            onNewSession={handleNewSession}
            onQueryChange={setQuery}
            onStagedItemsChange={setStagedItems}
            onSubmit={handleSubmit}
            onUpload={handleUpload}
            query={query}
            stagedItems={stagedItems}
            uploadedItems={session.uploadedItems}
          />
        ) : null}
      </main>
    </div>
  );
}
