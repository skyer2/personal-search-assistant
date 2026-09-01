import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { cancelTask, listSessionFiles, resumeHitl, startTask, uploadSessionFiles } from "../lib/api";
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
import type {
  ConnectionState,
  HitlInterruptPayload,
  MonitorMessage,
  OutputFile,
  SearchMode,
  SocketMessage,
  UploadedItem
} from "../types";

const MAX_EVENTS = 120;

function extractString(data: Record<string, unknown>, key: string): string | null {
  const value = data[key];
  return typeof value === "string" ? value : null;
}

export function useDeepAgentSession() {
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | undefined>(undefined);
  const heartbeatTimerRef = useRef<number | undefined>(undefined);
  const uploadedNameSetRef = useRef<Set<string>>(new Set());
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
    setThreadId(nextThreadId);
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
  }, []);

  const discardFailedTask = useCallback(() => {
    const nextThreadId = createThreadId();
    storeThreadId(nextThreadId);
    setThreadId(nextThreadId);
    setEvents([]);
    setFiles([]);
    setSessionPath("");
    setResult("");
    setLastError("");
    setHitlPending(null);
    setIsHitlSubmitting(false);
    setIsRunning(false);
    setIsCancelling(false);
    setTaskFailure(null);
    setElapsedClock(IDLE_ELAPSED_CLOCK);
    setElapsedNow(Date.now());
  }, []);

  const refreshFiles = useCallback(async () => {
    if (!sessionPath) {
      return;
    }

    const response = await listSessionFiles(sessionPath);
    if (response.error) {
      if (response.error.includes("拒绝访问")) {
        setSessionPath("");
        setFiles([]);
      }
      throw new Error(response.error);
    }
    setFiles(response.files || []);
  }, [sessionPath]);

  useEffect(() => {
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
          if (payload.type === "pong") {
            setLastPongAt(new Date().toISOString());
            return;
          }

          if (payload.type !== "monitor_event") {
            return;
          }

          setEvents((previous) => [...previous, payload].slice(-MAX_EVENTS));

          if (payload.event === "session_created") {
            const path = extractString(payload.data, "path");
            if (path) {
              setSessionPath(path);
            }
          }

          if (payload.event === "hitl_interrupt") {
            setElapsedClock((previous) => pauseElapsedClock(previous, Date.now()));
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
          }

          if (payload.event === "task_cancelled") {
            setElapsedClock((previous) => stopElapsedClock(previous, Date.now()));
            setResult((previous) => previous || payload.message);
            setIsRunning(false);
            setIsCancelling(false);
          }

          if (payload.event === "error") {
            setElapsedClock((previous) => stopElapsedClock(previous, Date.now()));
            setLastError(payload.message);
            setTaskFailure({ message: payload.message });
            setIsRunning(false);
            setIsCancelling(false);
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
  }, [clearSocketTimers, threadId]);

  useEffect(() => {
    if (!sessionPath) {
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
  }, [hitlPending, isRunning, refreshFiles, sessionPath]);

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
      setResult("");
      setLastError("");
      setHitlPending(null);
      setTaskFailure(null);
      try {
        const response = await startTask(cleanQuery, threadId, { mode });
        if (response.thread_id && response.thread_id !== threadId) {
          storeThreadId(response.thread_id);
          setThreadId(response.thread_id);
        }
        return response;
      } catch (error) {
        setElapsedClock(IDLE_ELAPSED_CLOCK);
        setIsRunning(false);
        setIsCancelling(false);
        throw error;
      }
    },
    [threadId]
  );

  const cancelCurrentTask = useCallback(async () => {
    if (!isRunning) {
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
        setResult((previous) => previous || "任务已取消");
      }
      return response;
    } catch (error) {
      setIsCancelling(false);
      throw error;
    }
  }, [isRunning, threadId]);

  const uploadFiles = useCallback(
    async (items: UploadedItem[]) => {
      if (items.length === 0) {
        throw new Error("请选择要上传的文件");
      }

      const nextItems = items.filter((item) => !uploadedNameSetRef.current.has(item.name));

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
          nextItems.map((item) => item.raw),
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
      } finally {
        setIsHitlSubmitting(false);
      }
    },
    [hitlPending, threadId]
  );

  const stats = useMemo(() => {
    const toolEvents = events.filter((event) => event.event === "tool_start").length;
    const assistantEvents = events.filter((event) => event.event === "assistant_call").length;
    const errorEvents = events.filter((event) => event.event === "error").length;

    return {
      toolEvents,
      assistantEvents,
      errorEvents,
      fileCount: files.length
    };
  }, [events, files.length]);

  const runStatus = useMemo(
    () =>
      deriveRunStatus({
        isRunning,
        isCancelling,
        hitlPending,
        events,
        result,
        taskFailed: Boolean(taskFailure)
      }),
    [events, hitlPending, isCancelling, isRunning, result, taskFailure]
  );

  useEffect(() => {
    if (!elapsedClockIsTicking(elapsedClock)) {
      return;
    }
    const timer = window.setInterval(() => {
      setElapsedNow(Date.now());
    }, 250);
    return () => window.clearInterval(timer);
  }, [elapsedClock]);

  const elapsedMs = readElapsedMs(elapsedClock, elapsedNow);

  return {
    connectionState,
    elapsedMs,
    events,
    files,
    isCancelling,
    isRunning,
    isUploading,
    hitlPending,
    isHitlSubmitting,
    taskFailure,
    lastError,
    lastPongAt,
    refreshFiles,
    resetSession,
    discardFailedTask,
    result,
    runStatus,
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
