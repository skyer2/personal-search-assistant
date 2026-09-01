"""
Agent 执行过程监控模块

负责把工具调用、子智能体调用、任务结果和会话目录等事件统一包装后推送给前端
在 Web 服务中优先通过 WebSocket 定向推送；在脚本调试场景中保留控制台输出
"""

import asyncio
import builtins
import datetime
from typing import Any, Optional

from fastapi import WebSocket

from app.api.context import get_thread_context


class ToolMonitor:
    """
    工具和助手调用的统一监控入口

    业务工具由 Harness astream 统一 report_tool；工具函数内不要再报一遍，避免中英双计。
    具体是通过 WebSocket 推送，还是输出到脚本运行时，由本类内部统一处理
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ToolMonitor, cls).__new__(cls)
            cls._instance.websocket_manager = None
        return cls._instance

    def set_websocket_manager(self, manager: "ConnectionManager") -> None:
        """绑定 FastAPI WebSocket 连接管理器"""
        self.websocket_manager = manager

    def _emit(
        self,
        event_type: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        构造统一监控事件，并尝试推送到当前 thread_id 对应的前端连接

        :param event_type: 事件类型，例如 tool_start、assistant_call
        :param message: 面向前端展示的事件说明
        :param data: 附加结构化数据
        """
        payload = {
            "type": "monitor_event",
            "event": event_type,
            "message": message,
            "data": data or {},
            "timestamp": datetime.datetime.now().isoformat(),
        }

        if self.websocket_manager:
            try:
                thread_id = get_thread_context()
                manager_loop = self.websocket_manager.loop

                if manager_loop and thread_id:
                    self._send_to_websocket(payload, thread_id, manager_loop)
            except Exception as e:
                print(f"[Monitor] WebSocket send failed: {e}")

        # DeepAgents 脚本调试时，如果运行时暴露了 stream_writer，也同步写入流式输出
        if hasattr(builtins, "runtime") and hasattr(builtins.runtime, "stream_writer"):
            try:
                builtins.runtime.stream_writer(payload)
            except Exception:
                pass

        # 控制台保底输出，便于无前端场景下观察执行过程
        print(f"\n[Monitor:{event_type}] {message}")

    def forward_canonical_event(
        self,
        event_type: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        """Flight Recorder → UI。不要再走 report_*，避免二次语义化。"""
        self._emit(event_type, message, data)

    def _send_to_websocket(
        self,
        payload: dict[str, Any],
        thread_id: str,
        manager_loop: asyncio.AbstractEventLoop,
    ) -> None:
        """
        将监控事件投递到 WebSocket 所在事件循环

        FastAPI 的 WebSocket 必须在创建它的事件循环中发送消息
        如果当前代码已经在同一个循环里，直接 create_task；否则使用线程安全投递
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        coroutine = self.websocket_manager.send_to_thread(payload, thread_id)
        if current_loop and current_loop == manager_loop:
            current_loop.create_task(coroutine)
        else:
            asyncio.run_coroutine_threadsafe(coroutine, manager_loop)

    def report_tool(
        self,
        tool_name: str,
        args: Optional[dict[str, Any]] = None,
        *,
        tool_call_id: str = "",
    ) -> None:
        """报告开始执行某个工具。运行中由 Flight Recorder 统一 emit，避免双计。"""
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                recorder.emit(
                    EventType.TOOL_STARTED,
                    phase="execute",
                    status="start",
                    attributes={
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "args": args or {},
                    },
                )
                return
        except Exception:
            pass
        self._emit(
            "tool_start",
            f"开始执行工具: {tool_name}",
            {"tool_name": tool_name, "args": args, "tool_call_id": tool_call_id},
        )

    def report_tool_end(
        self,
        tool_name: str,
        *,
        tool_call_id: str = "",
        duration_ms: int | None = None,
        status: str = "ok",
        error: str = "",
    ) -> None:
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            event_type = EventType.TOOL_COMPLETED if status == "ok" else EventType.TOOL_FAILED
            if recorder.is_active:
                recorder.emit(
                    event_type,
                    phase="execute",
                    status=status,
                    duration_ms=duration_ms,
                    attributes={
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "error": error,
                    },
                )
                return
        except Exception:
            pass
        event_name = "tool_end" if status == "ok" else "tool_error"
        self._emit(
            event_name,
            f"工具{'完成' if status == 'ok' else '失败'}: {tool_name}",
            {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "duration_ms": duration_ms,
                "status": status,
                "error": error,
            },
        )

    def report_assistant(
        self,
        assistant_name: str,
        args: Optional[dict[str, Any]] = None,
    ) -> None:
        """报告正在调用某个子智能体"""
        self._emit(
            "assistant_call",
            f"正在调用助手: {assistant_name}",
            {"assistant_name": assistant_name, "args": args},
        )

    def report_task_result(self, result: str) -> None:
        """报告任务最终结果"""
        self._emit("task_result", "任务执行完成", {"result": result})

    def report_task_cancelled(self) -> None:
        """报告任务已被用户取消"""
        self._emit("task_cancelled", "任务已取消")

    def report_session_dir(self, path: str) -> None:
        """报告当前任务工作目录"""
        self._emit("session_created", f"工作目录已创建: {path}", {"path": path})

    def report_phase(
        self,
        phase: str,
        status: str,
        **data: Any,
    ) -> None:
        """报告 Harness 显式 Loop 阶段事件（understand / plan / execute / validate 等）"""
        status_icon = {"start": "→", "done": "✓", "failed": "✗", "cancelled": "⊗"}.get(
            status, "·"
        )
        try:
            from app.observability import get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                recorder.emit_phase(
                    phase,
                    status,
                    task_id=data.get("task_id"),
                    attempt=data.get("attempt"),
                    plan_version=data.get("plan_version"),
                    duration_ms=data.get("duration_ms"),
                    attributes={k: v for k, v in data.items() if k not in {"status"}},
                )
                return
        except Exception:
            pass
        self._emit(
            "phase",
            f"[{phase}] {status_icon} {status}",
            {"phase": phase, "status": status, **data},
        )

    def report_hitl_interrupt(
        self,
        session_id: str,
        action_requests: list[dict[str, Any]],
        review_configs: list[dict[str, Any]],
        **extra: Any,
    ) -> None:
        """报告 interrupt_on 命中，等待人工审批。"""
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                recorder.emit(
                    EventType.HITL_INTERRUPT,
                    phase="hitl",
                    status="waiting",
                    attributes={
                        "gate_type": extra.get("gate_type"),
                        "action_count": len(action_requests),
                    },
                    to_ws=False,
                )
        except Exception:
            pass
        self._emit(
            "hitl_interrupt",
            f"等待人工审批（{len(action_requests)} 个动作）",
            {
                "session_id": session_id,
                "action_requests": action_requests,
                "review_configs": review_configs,
                **extra,
            },
        )


monitor = ToolMonitor()


class ConnectionManager:
    """
    WebSocket 连接管理器

    active_connections 使用 thread_id 作为 key，保证监控事件只推送给对应任务的前端连接
    """

    def __init__(self) -> None:
        self.active_connections: dict[str, WebSocket] = {}
        # WebSocket 发送必须回到创建连接的事件循环，因此启动时需要显式绑定 loop
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定 FastAPI 主事件循环，并同步注册到 monitor"""
        self.loop = loop
        monitor.set_websocket_manager(self)
        print(f"[Monitor] ConnectionManager manually bound to loop: {id(self.loop)}")

    async def connect(self, websocket: WebSocket, thread_id: str) -> None:
        """接受 WebSocket 连接，并按 thread_id 保存"""
        await websocket.accept()
        self.active_connections[thread_id] = websocket
        print(f"Client connected: {thread_id}")

    def disconnect(self, websocket: WebSocket, thread_id: str) -> None:
        """移除已经断开的 WebSocket 连接"""
        if self.active_connections.get(thread_id) is websocket:
            del self.active_connections[thread_id]
            print(f"Client disconnected: {thread_id}")
        else:
            print(f"Stale websocket disconnected, current connection kept: {thread_id}")

    async def send_personal_message(self, message: str, websocket: WebSocket) -> None:
        """向指定 WebSocket 发送纯文本消息"""
        await websocket.send_text(message)

    async def send_to_thread(self, message: dict[str, Any], thread_id: str) -> None:
        """向指定 thread_id 对应的前端连接发送 JSON 消息"""
        if thread_id in self.active_connections:
            websocket = self.active_connections[thread_id]
            await websocket.send_json(message)


manager = ConnectionManager()
