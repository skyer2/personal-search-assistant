import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChatTurn } from "../components/ConversationThread";
import {
  cancelTask,
  fetchRunEvents,
  fetchSessionBootstrap,
  listSessionArtifacts,
  resumeHitl,
  startTask,
  uploadSessionFiles
} from "../lib/api";
import { WS_BASE_URL, wsUrl } from "../lib/config";
import { createThreadId, getStoredThreadId, storeThreadId } from "../lib/thread";
import {
  IDLE_ELAPSED_CLOCK,
  elapsedClockIsTicking,
  pauseElapsedClock,
  readElapsedMs,
  resumeElapsedClock,
  startElapsedClock,
  stopElapsedClock
} from "../lib/elapsedClock";
import { deriveRunStatus } from "../lib/runStatus";
import {
  clockFromRun,
  eventDedupeKey,
  hitlFromBootstrap,
  isActiveServerStatus,
  mergeMonitorEvents,
  turnsFromBootstrap,
  uploadsFromBootstrap
} from "../lib/sessionProjection";
import type {
  ConnectionState,
  HitlInterruptPayload,
  MonitorMessage,
  OutputFile,
  SearchMode,
  SocketMessage,
  UploadedItem
} from "../types";

const VIEW_EVENT_WINDOW = 120;

function extractString(data: Record<string, unknown>, key: string): string | null {
  const value = data[key];
  return typeof value === "string" ? value : null;
}

function messageSeq(message: MonitorMessage): number {
  if (typeof message.seq === "number") {
    return message.seq;
  }
  return typeof message.data?.seq === "number" ? Number(message.data.seq) : 0;
}

export function useDeepAgentSession() {
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | undefined>(undefined);
  const heartbeatTimerRef = useRef<number | undefined>(undefined);
  const uploadedNameSetRef = useRef<Set<string>>(new Set());
  const seenEventKeysRef = useRef<Set<string>>(new Set());
  const lastEventSeqRef = useRef(0);
  const currentRunIdRef = useRef("");
  const statsBaseRef = useRef({ tool: 0, assistant: 0, errors: 0 });
  const [threadId, setThreadId] = useState(getStoredThreadId);
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [events, setEvents] = useState<MonitorMessage[]>([]);
  const [files, setFiles] = useState<OutputFile[]>([]);
  const [sessionPath, setSessionPath] = useState("");
  const [result, setResult] = useState("");
  const [lastError, setLastError] = useState("");
  const [lastPongAt, setLastPongAt] = useState("");
  const [isRunning, setIsRunning] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadedItems, setUploadedItems] = useState<UploadedItem[]>([]);
  const [hitlPending, setHitlPending] = useState<HitlInterruptPayload | null>(null);
  const [isHitlSubmitting, setIsHitlSubmitting] = useState(false);
  const [taskFailure, setTaskFailure] = useState<{ message: string } | null>(null);
  const [elapsedClock, setElapsedClock] = useState(IDLE_ELAPSED_CLOCK);
  const [elapsedNow, setElapsedNow] = useState(() => Date.now());
  const [hydrated, setHydrated] = useState(false);
  const [sessionFound, setSessionFound] = useState(true);
  const [bootstrapNotice, setBootstrapNotice] = useState("");
  const [initialTurns, setInitialTurns] = useState<ChatTurn[]>([]);
  const [currentRunId, setCurrentRunId] = useState("");
  const [serverStatus, setServerStatus] = useState("");
  const [hasMoreEvents, setHasMoreEvents] = useState(false);
  const [liveStats, setLiveStats] = useState({ tool: 0, assistant: 0, errors: 0 });

  const applyCurrentRun = useCallback((runId: string) => {
    currentRunIdRef.current = runId;
    setCurrentRunId(runId);
  }, []);

  const ingestEvents = useCallback((incoming: MonitorMessage[]) => {
    if (incoming.length === 0) {
      return;
    }
    const delta = { tool: 0, assistant: 0, errors: 0 };
    incoming.forEach((message) => {
      const seq = messageSeq(message);
      if (seq > lastEventSeqRef.current) {
        lastEventSeqRef.current = seq;
      }
      const replayed = Boolean(message.replay || message.data?.replay);
      if (!replayed) {
        if (message.event === "tool_start") {
          delta.tool += 1;
        }
        if (message.event === "assistant_call" || message.event === "worker") {
          if (message.event === "assistant_call" || /\[worker\] start/.test(message.message)) {
            delta.assistant += 1;
          }
        }
        if (message.event === "error" || message.event === "tool_error") {
          delta.errors += 1;
        }
      }
    });
    if (delta.tool || delta.assistant || delta.errors) {
      setLiveStats((previous) => ({
        tool: previous.tool + delta.tool,
        assistant: previous.assistant + delta.assistant,
        errors: previous.errors + delta.errors
      }));
    }
    setEvents((previous) => mergeMonitorEvents(previous, incoming, seenEventKeysRef.current, VIEW_EVENT_WINDOW));
  }, []);

  const resetProjection = useCallback(() => {
    seenEventKeysRef.current.clear();
    lastEventSeqRef.current = 0;
    currentRunIdRef.current = "";
    statsBaseRef.current = { tool: 0, assistant: 0, errors: 0 };
    setEvents([]);
    setFiles([]);
    setSessionPath("");
    setResult("");
    setLastError("");
    setUploadedItems([]);
    uploadedNameSetRef.current.clear();
    setIsRunning(false);
    setIsCancelling(false);
    setHitlPending(null);
    setIsHitlSubmitting(false);
    setTaskFailure(null);
    setElapsedClock(IDLE_ELAPSED_CLOCK);
    setElapsedNow(Date.now());
    setInitialTurns([]);
    setCurrentRunId("");
    setServerStatus("");
    setHasMoreEvents(false);
    setLiveStats({ tool: 0, assistant: 0, errors: 0 });
    setSessionFound(true);
    setBootstrapNotice("");
    setHydrated(false);
  }, []);

  const clearSocketTimers = useCallback(() => {
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = undefined;
    }
    if (heartbeatTimerRef.current) {
      window.clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = undefined;
    }
  }, []);

  const resetSession = useCallback(() => {
    const nextThreadId = createThreadId();
    storeThreadId(nextThreadId);
    resetProjection();
    setThreadId(nextThreadId);
  }, [resetProjection]);

  const discardFailedTask = useCallback(() => {
    const nextThreadId = createThreadId();
    storeThreadId(nextThreadId);
    resetProjection();
    setThreadId(nextThreadId);
  }, [resetProjection]);

  const refreshFiles = useCallback(async () => {
    const response = await listSessionArtifacts(threadId);
    if (response.error) {
      if (response.error.includes("拒绝访问")) {
        setSessionPath("");
        setFiles([]);
      }
      throw new Error(response.error);
    }
    setFiles(response.files || []);
  }, [threadId]);

  const hydrateFromServer = useCallback(async () => {
    setHydrated(false);
    try {
      const data = await fetchSessionBootstrap(threadId);
      setSessionFound(data.found);
      setBootstrapNotice(data.found ? "" : data.notice || "session_not_found");
      if (!data.found) {
        setInitialTurns([]);
        setHydrated(true);
        return;
      }
      const current = data.current_run;
      applyCurrentRun(current?.run_id || "");
      lastEventSeqRef.current = data.last_event_seq || 0;
      seenEventKeysRef.current.clear();
      (data.events || []).forEach((message) => {
        const key = eventDedupeKey(message);
        if (key) {
          seenEventKeysRef.current.add(key);
        }
      });
      setEvents((data.events || []).slice(-VIEW_EVENT_WINDOW));
      setHasMoreEvents((data.last_event_seq || 0) > (data.events || []).length);
      setFiles(data.output_files || []);
      setSessionPath(current?.session_workspace || `session_${threadId}`);
      setResult(current?.final_result || "");
      setHitlPending(hitlFromBootstrap(data));
      setUploadedItems(uploadsFromBootstrap(data));
      uploadsFromBootstrap(data).forEach((item) => uploadedNameSetRef.current.add(item.name));
      const active = isActiveServerStatus(current?.status);
      setIsRunning(active);
      setIsCancelling(current?.status === "cancelling");
      setServerStatus(current?.status || "");
      setElapsedClock(clockFromRun(current));
      setElapsedNow(Date.now());
      statsBaseRef.current = {
        tool: data.stats?.tool_calls || 0,
        assistant: data.stats?.assistant_calls || 0,
        errors: data.stats?.errors || 0
      };
      setLiveStats({ tool: 0, assistant: 0, errors: 0 });
      setInitialTurns(turnsFromBootstrap(data));
      if (current?.status === "failed" && current.error) {
        setLastError(current.error);
      }
    } catch (error) {
      setLastError(error instanceof Error ? error.message : "会话恢复失败");
      setSessionFound(false);
      setBootstrapNotice("bootstrap_failed");
    } finally {
      setHydrated(true);
    }
  }, [applyCurrentRun, threadId]);

  useEffect(() => {
    void hydrateFromServer();
  }, [hydrateFromServer]);

  useEffect(() => {
    if (!hydrated) {
      return;
    }
    let disposed = false;

    function connect() {
      clearSocketTimers();
      const hadSocket = Boolean(socketRef.current);
      socketRef.current?.close();
      setConnectionState(hadSocket ? "reconnecting" : "connecting");

      const socket = new WebSocket(wsUrl(threadId));
      socketRef.current = socket;

      socket.onopen = () => {
        if (disposed) {
          return;
        }
        setConnectionState("connected");
        setLastError("");
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(
            JSON.stringify({
              type: "subscribe",
              run_id: currentRunIdRef.current,
              after_seq: lastEventSeqRef.current
            })
          );
        }
        heartbeatTimerRef.current = window.setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) {
            socket.send("ping");
          }
        }, 25000);
      };

      socket.onmessage = (event) => {
        if (socketRef.current !== socket) {
          return;
        }
        try {
          const payload = JSON.parse(event.data) as SocketMessage;
          if (payload.type === "pong" || payload.type === "replay_complete") {
            if (payload.type === "pong") {
              setLastPongAt(new Date().toISOString());
            }
            return;
          }

          if (payload.type !== "monitor_event") {
            return;
          }

          ingestEvents([payload]);

          if (payload.event === "session_created") {
            const path = extractString(payload.data, "path");
            if (path) {
              setSessionPath(path);
            }
          }

          if (payload.event === "hitl_interrupt") {
            setElapsedClock((previous) => pauseElapsedClock(previous, Date.now()));
            setServerStatus("awaiting_approval");
            setHitlPending({
              session_id: extractString(payload.data, "session_id") || threadId,
              action_requests: Array.isArray(payload.data.action_requests)
                ? (payload.data.action_requests as HitlInterruptPayload["action_requests"])
                : [],
              review_configs: Array.isArray(payload.data.review_configs)
                ? (payload.data.review_configs as HitlInterruptPayload["review_configs"])
                : [],
              step_index:
                typeof payload.data.step_index === "number"
                  ? payload.data.step_index
                  : undefined,
              gate_type:
                typeof payload.data.gate_type === "string"
                  ? payload.data.gate_type
                  : undefined,
              editable: Boolean(payload.data.editable)
            });
          }

          if (payload.event === "task_result") {
            const finalResult = extractString(payload.data, "result");
            setElapsedClock((previous) => stopElapsedClock(previous, Date.now()));
            setResult(finalResult || payload.message);
            setIsRunning(false);
            setIsCancelling(false);
            setServerStatus("completed");
            setHitlPending(null);
            void refreshFiles().catch(() => undefined);
          }

          if (payload.event === "task_cancelled") {
            setElapsedClock((previous) => stopElapsedClock(previous, Date.now()));
            setResult((previous) => previous || payload.message);
            setIsRunning(false);
            setIsCancelling(false);
            setServerStatus("interrupted");
            void refreshFiles().catch(() => undefined);
          }

          if (payload.event === "error") {
            setElapsedClock((previous) => stopElapsedClock(previous, Date.now()));
            setLastError(payload.message);
            setTaskFailure({ message: payload.message });
            setIsRunning(false);
            setIsCancelling(false);
            setServerStatus("failed");
            void refreshFiles().catch(() => undefined);
          }
        } catch (error) {
          setLastError(error instanceof Error ? error.message : "WebSocket 消息解析失败");
        }
      };

      socket.onerror = () => {
        if (!disposed && socketRef.current === socket) {
          setLastError(
            `WebSocket 连接失败 (${WS_BASE_URL}/ws/...)，请检查 ENDPOINTS 配置与后端 8000 端口`
          );
        }
      };

      socket.onclose = () => {
        if (socketRef.current !== socket) {
          return;
        }
        clearSocketTimers();
        if (disposed) {
          setConnectionState("closed");
          return;
        }
        setConnectionState("reconnecting");
        reconnectTimerRef.current = window.setTimeout(connect, 2000);
      };
    }

    connect();

    return () => {
      disposed = true;
      clearSocketTimers();
      socketRef.current?.close();
    };
  }, [clearSocketTimers, ingestEvents, refreshFiles, threadId, hydrated]);

  useEffect(() => {
    if (!hydrated) {
      return;
    }

    refreshFiles().catch((error: unknown) => {
      setLastError(error instanceof Error ? error.message : "文件列表刷新失败");
    });

    const timer = window.setInterval(() => {
      refreshFiles().catch((error: unknown) => {
        setLastError(error instanceof Error ? error.message : "文件列表刷新失败");
      });
    }, isRunning && !hitlPending ? 2500 : 8000);

    return () => window.clearInterval(timer);
  }, [hitlPending, hydrated, isRunning, refreshFiles]);

  const submitTask = useCallback(
    async (query: string, mode: SearchMode = "agent") => {
      const cleanQuery = query.trim();
      if (!cleanQuery) {
        throw new Error("请输入研究任务");
      }

      const startedAt = Date.now();
      setElapsedNow(startedAt);
      setElapsedClock(startElapsedClock(startedAt));
      setIsRunning(true);
      setIsCancelling(false);
      setEvents([]);
      seenEventKeysRef.current.clear();
      lastEventSeqRef.current = 0;
      setResult("");
      setLastError("");
      setHitlPending(null);
      setTaskFailure(null);
      setServerStatus("running");
      statsBaseRef.current = { tool: 0, assistant: 0, errors: 0 };
      setLiveStats({ tool: 0, assistant: 0, errors: 0 });
      try {
        const response = await startTask(cleanQuery, threadId, { mode });
        if (response.thread_id && response.thread_id !== threadId) {
          storeThreadId(response.thread_id);
          setThreadId(response.thread_id);
        }
        if (response.run_id) {
          applyCurrentRun(response.run_id);
        }
        setSessionFound(true);
        setBootstrapNotice("");
        return response;
      } catch (error) {
        setElapsedClock(IDLE_ELAPSED_CLOCK);
        setIsRunning(false);
        setIsCancelling(false);
        setServerStatus("");
        throw error;
      }
    },
    [applyCurrentRun, threadId]
  );

  const cancelCurrentTask = useCallback(async () => {
    if (!isRunning && serverStatus !== "awaiting_approval" && serverStatus !== "recoverable") {
      throw new Error("当前没有正在执行的任务");
    }

    setIsCancelling(true);
    setLastError("");
    try {
      const response = await cancelTask(threadId);
      if (response.status === "cancelled") {
        setElapsedClock((previous) => stopElapsedClock(previous, Date.now()));
        setIsRunning(false);
        setIsCancelling(false);
        setServerStatus("interrupted");
        setResult((previous) => previous || "任务已取消");
      }
      return response;
    } catch (error) {
      setIsCancelling(false);
      throw error;
    }
  }, [isRunning, serverStatus, threadId]);

  const uploadFiles = useCallback(
    async (items: UploadedItem[]) => {
      if (items.length === 0) {
        throw new Error("请选择要上传的文件");
      }

      const nextItems = items.filter((item) => item.raw && !uploadedNameSetRef.current.has(item.name));

      if (nextItems.length === 0) {
        return {
          status: "uploaded",
          files: Array.from(uploadedNameSetRef.current)
        };
      }

      setIsUploading(true);
      setLastError("");
      try {
        const response = await uploadSessionFiles(
          nextItems.map((item) => item.raw as File),
          threadId
        );
        setUploadedItems((previous) => {
          const names = new Set(previous.map((item) => item.name));
          const next = [...previous];
          nextItems.forEach((item) => {
            if (!names.has(item.name)) {
              names.add(item.name);
              uploadedNameSetRef.current.add(item.name);
              next.push(item);
            }
          });
          return next;
        });
        return response;
      } finally {
        setIsUploading(false);
      }
    },
    [threadId]
  );

  const submitHitlDecisions = useCallback(
    async (
      decisions: Array<{
        type: "approve" | "reject" | "edit";
        edited_action?: Record<string, unknown>;
      }>
    ) => {
      if (!hitlPending) {
        throw new Error("当前没有待审批动作");
      }
      setIsHitlSubmitting(true);
      setLastError("");
      try {
        const count = hitlPending.action_requests.length || decisions.length;
        const normalized =
          decisions.length === count
            ? decisions
            : Array.from({ length: count }, () => decisions[0] || { type: "approve" as const });
        await resumeHitl(threadId, normalized);
        setElapsedClock((previous) => resumeElapsedClock(previous, Date.now()));
        setHitlPending(null);
        setIsRunning(true);
        setServerStatus("running");
      } finally {
        setIsHitlSubmitting(false);
      }
    },
    [hitlPending, threadId]
  );

  const loadOlderEvents = useCallback(async () => {
    const runId = currentRunIdRef.current;
    if (!runId) {
      return;
    }
    const oldest = events.reduce((min, message) => {
      const seq = messageSeq(message);
      return seq > 0 && (min === 0 || seq < min) ? seq : min;
    }, 0);
    if (oldest <= 1) {
      setHasMoreEvents(false);
      return;
    }
    const response = await fetchRunEvents(runId, { beforeSeq: oldest, limit: VIEW_EVENT_WINDOW });
    const incoming = response.events || [];
    incoming.forEach((message) => {
      const key = eventDedupeKey(message);
      if (key) {
        seenEventKeysRef.current.add(key);
      }
    });
    setEvents((previous) => {
      const keys = new Set(previous.map((item) => eventDedupeKey(item)).filter(Boolean));
      const prepended = incoming.filter((item) => {
        const key = eventDedupeKey(item);
        return !key || !keys.has(key);
      });
      return [...prepended, ...previous];
    });
    setHasMoreEvents(incoming.length >= VIEW_EVENT_WINDOW);
  }, [events, ingestEvents]);

  const stats = useMemo(
    () => ({
      toolEvents: statsBaseRef.current.tool + liveStats.tool,
      assistantEvents: statsBaseRef.current.assistant + liveStats.assistant,
      errorEvents: statsBaseRef.current.errors + liveStats.errors,
      fileCount: files.length
    }),
    [files.length, liveStats]
  );

  const runStatus = useMemo(
    () =>
      deriveRunStatus({
        isRunning,
        isCancelling,
        hitlPending,
        events,
        result,
        taskFailed: Boolean(taskFailure),
        serverStatus
      }),
    [events, hitlPending, isCancelling, isRunning, result, serverStatus, taskFailure]
  );

  useEffect(() => {
    if (!elapsedClockIsTicking(elapsedClock)) {
      return;
    }
    const timer = window.setInterval(() => {
      setElapsedNow(Date.now());
    }, 1000);
    return () => window.clearInterval(timer);
  }, [elapsedClock]);

  const elapsedMs = readElapsedMs(elapsedClock, elapsedNow);

  return {
    bootstrapNotice,
    connectionState,
    currentRunId,
    elapsedMs,
    events,
    files,
    hasMoreEvents,
    hydrated,
    isCancelling,
    isRunning,
    isUploading,
    hitlPending,
    isHitlSubmitting,
    initialTurns,
    taskFailure,
    lastError,
    lastPongAt,
    loadOlderEvents,
    refreshFiles,
    resetSession,
    discardFailedTask,
    result,
    runStatus,
    sessionFound,
    sessionPath,
    stats,
    cancelCurrentTask,
    submitTask,
    submitHitlDecisions,
    threadId,
    uploadFiles,
    uploadedItems
  };
}
