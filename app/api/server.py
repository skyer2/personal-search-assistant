"""
FastAPI 接口层与项目闭环入口

负责承接前端的任务提交、任务取消、文件上传/下载、输出文件列表查询和
WebSocket 长连接。HTTP 接口只做轻量调度，真正的 DeepAgents 执行放到后台
任务中；执行进度、工具调用和最终结果由 monitor 按 thread_id 推送给前端。

部署不变量：single backend process。active_tasks / HITL Future / WebSocket
fanout / RunJournal 都是进程内 cache，不要用 uvicorn --workers > 1。
"""

import asyncio
import json
import shutil
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

import uvicorn
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.agent.harness.hitl import hitl_coordinator
from app.agent.main_agent import run_deep_agent
from app.api.eval_routes import router as eval_router
from app.api.harness_routes import router as harness_router
from app.api.health import collect_health
from app.api.metrics_routes import router as metrics_router
from app.api.meta import router as meta_router
from app.api.monitor import manager
from app.api.session_routes import router as session_router
from app.api.trace_routes import router as trace_routes
from app.observability.events import new_id
from app.observability.recorder import get_recorder
from app.observability.replay import load_wire_events
from app.run_store import get_run_store


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    服务生命周期入口。

    启动时绑定当前事件循环到 WebSocket 管理器，确保后台 Agent 任务可以把
    monitor 事件投递回 FastAPI 所在的 loop。
    并把崩溃前仍标记 RUNNING 的 Run 标成 recoverable（不自动续跑）。
    """
    loop = asyncio.get_running_loop()
    manager.set_loop(loop)
    store = get_run_store()
    try:
        get_recorder().add_listener(store.on_event)
    except Exception as exc:
        print(f"[Server] RunStore listener skipped: {exc}")
    recovered = store.recover_stale_runs(set())
    if recovered:
        print(f"[Server] marked {len(recovered)} run(s) recoverable after restart")
    print(f"[Server] WebSocket Manager bound to loop: {id(loop)}")
    yield


# 当前文件位于 app/api/server.py，运行时目录统一收敛到 app 目录
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent

app = FastAPI(title="Research Agent Harness", lifespan=lifespan)
_http_logger = logging.getLogger("app.http")


@app.middleware("http")
async def log_not_found_requests(request, call_next):
    """Make otherwise opaque SVG/icon/API 404s actionable in development logs."""
    started = time.perf_counter()
    response = await call_next(request)
    if response.status_code == 404:
        _http_logger.warning(
            "HTTP 404 method=%s url=%s referer=%s duration_ms=%d",
            request.method,
            request.url,
            request.headers.get("referer", ""),
            int((time.perf_counter() - started) * 1000),
        )
    return response


app.include_router(eval_router)
app.include_router(harness_router)
app.include_router(trace_routes)
app.include_router(metrics_router)
app.include_router(meta_router)
app.include_router(session_router)

# 保存 thread_id -> 后台 Agent 任务，用于同一会话任务替换和主动取消
active_tasks: dict[str, asyncio.Task] = {}

# output 保存每个会话最终工作区，前端只允许从这里浏览和下载生成文件
output_dir = project_root / "output"
output_dir.mkdir(exist_ok=True)

# updated 暂存用户上传文件，run_deep_agent 启动时会复制到对应 output/session_xxx
updated_dir = project_root / "updated"
updated_dir.mkdir(exist_ok=True)

# 教学项目通常前后端分别本地启动，这里放开跨域以便 Vite 页面直接调用 API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TaskRequest(BaseModel):
    """前端启动任务时提交的请求体。"""

    query: str
    thread_id: str = None
    mode: str = "agent"
    user_id: str = "me"
    tenant_id: str = "local"
    project_id: str = "Inbox"


class HitlDecision(BaseModel):
    type: str
    edited_action: dict | None = None


class HitlResumeRequest(BaseModel):
    decisions: List[HitlDecision]


def _forget_task(thread_id: str, task: asyncio.Task) -> None:
    """
    清理已结束任务的登记关系。

    done_callback 触发时，active_tasks 中可能已经被新任务替换；只有仍是同一个
    task 时才删除，避免误清理同 thread_id 下刚启动的新任务。
    """
    if active_tasks.get(thread_id) is task:
        active_tasks.pop(thread_id, None)


@app.get("/health")
async def health_check():
    """Harness 健康检查：LLM / Tavily / Langfuse。"""
    return await collect_health()


@app.post("/api/task")
async def run_task(request: TaskRequest):
    """
    启动一次 DeepAgents 后台任务。

    HTTP 请求只负责创建后台协程并立即返回，后续执行轨迹、子智能体调用和最终
    答案都会由 monitor 通过 `/ws/{thread_id}` 推送给同一会话的前端。
    """
    thread_id = request.thread_id or str(uuid.uuid4())
    run_id = new_id(16)
    store = get_run_store()
    store.ensure_session(thread_id)
    previous = store.latest_run(thread_id)
    store.create_run(
        run_id=run_id,
        session_id=thread_id,
        query=request.query,
        mode=request.mode or "agent",
        session_workspace=f"session_{thread_id}",
    )

    # 同一个 thread_id 只保留一个活跃任务，新任务会先取消旧任务，避免并发写同一会话目录
    old_task = active_tasks.get(thread_id)
    if old_task and not old_task.done():
        if previous and previous.run_id != run_id:
            store.interrupt_run(previous.run_id, error="replaced by new run")
        old_task.cancel()

    # create_task 把长耗时 Agent 执行交给事件循环，接口本身不用等待最终结果
    task = asyncio.create_task(
        run_deep_agent(
            request.query,
            thread_id,
            user_id=request.user_id or "me",
            tenant_id=request.tenant_id or "local",
            project_id=request.project_id or "Inbox",
            mode=request.mode or "agent",
            run_id=run_id,
        )
    )
    active_tasks[thread_id] = task
    task.add_done_callback(lambda finished_task: _forget_task(thread_id, finished_task))

    return {"status": "started", "thread_id": thread_id, "run_id": run_id}


@app.get("/api/task/{thread_id}/hitl/pending")
async def get_hitl_pending(thread_id: str):
    """查询当前会话是否有待审批的 HITL 中断。"""
    pending = hitl_coordinator.get_pending(thread_id)
    if not pending:
        latest = get_run_store().latest_run(thread_id)
        if latest and latest.hitl_payload:
            pending = dict(latest.hitl_payload)
            pending.setdefault("run_id", latest.run_id)
    if not pending:
        raise HTTPException(status_code=404, detail="当前无待审批动作")
    return {"thread_id": thread_id, "pending": pending}


@app.post("/api/task/{thread_id}/resume")
async def resume_task(thread_id: str, request: HitlResumeRequest):
    """
    HITL 人工审批恢复。

    decisions 顺序须与 action_requests 一致，每项 type 为 approve / reject / edit。
    """
    decisions = [item.model_dump(exclude_none=True) for item in request.decisions]
    accepted = hitl_coordinator.submit_decisions(thread_id, decisions)
    if not accepted:
        latest = get_run_store().latest_run(thread_id)
        if latest and latest.hitl_payload:
            raise HTTPException(
                status_code=409,
                detail="HITL 审批仍在，但当前进程没有等待中的 Future（后端可能已重启）。请把该 run 视为 recoverable。",
            )
        raise HTTPException(status_code=404, detail="未找到待恢复的 HITL 会话")
    return {"status": "resumed", "thread_id": thread_id, "decisions": decisions}


@app.post("/api/task/{thread_id}/cancel")
async def cancel_task(thread_id: str):
    """
    取消指定 thread_id 对应的后台 Agent 任务。

    注意：取消会向 asyncio.Task 注入 CancelledError。若底层第三方工具正在执行不可中断
    的同步阻塞调用，任务可能需要等该调用返回后才会真正结束。
    """
    task = active_tasks.get(thread_id)
    latest = get_run_store().latest_run(thread_id)
    if latest:
        get_run_store().mark_cancelling(latest.run_id)
    if not task or task.done():
        active_tasks.pop(thread_id, None)
        if latest and latest.status in {
            "running",
            "awaiting_approval",
            "cancelling",
            "queued",
            "recoverable",
        }:
            get_run_store().interrupt_run(
                latest.run_id, error="cancelled with no live task"
            )
            return {
                "status": "cancelled",
                "thread_id": thread_id,
                "run_id": latest.run_id,
            }
        raise HTTPException(status_code=404, detail="任务不存在或已结束")

    # 先发出取消信号，再短暂等待协程响应；若底层阻塞中，则返回 cancelling 给前端继续展示状态
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        _forget_task(thread_id, task)
        return {"status": "cancelled", "thread_id": thread_id}
    except asyncio.TimeoutError:
        return {"status": "cancelling", "thread_id": thread_id}
    except Exception as e:
        _forget_task(thread_id, task)
        return {"status": "cancelled", "thread_id": thread_id, "message": str(e)}

    _forget_task(thread_id, task)
    return {"status": "cancelled", "thread_id": thread_id}


@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...), thread_id: str = Form(...)):
    """
    文件上传接口 (File Upload)。

    目标：
    1. 接收用户上传的一个或多个文件。
    2. 保存到 `updated/session_{thread_id}` 目录。
    3. 供 Agent 在后续任务中读取和分析。

    Args:
        files (List[UploadFile]): 文件对象列表。
        thread_id (str): 关联的任务会话 ID。
    """
    # 上传文件先按会话隔离保存，避免不同任务读取到彼此的附件
    target_dir = updated_dir / f"session_{thread_id}"
    target_dir.mkdir(parents=True, exist_ok=True)

    saved_files = []
    for file in files:
        file_path = target_dir / file.filename
        # 直接复制文件流，避免大文件一次性读入内存
        with file_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        saved_files.append(file.filename)
        try:
            get_run_store().add_upload(
                thread_id,
                file.filename,
                file_path.stat().st_size,
                server_path=file.filename,
            )
        except Exception as exc:
            print(f"[Upload] RunStore metadata skipped: {exc}")

    return {"status": "uploaded", "files": saved_files}


@app.get("/api/download")
async def download_file(path: str):
    """
    文件下载接口 (File Download)。

    目标：
    1. 根据绝对路径下载文件。
    2. 严格的安全检查，防止越权访问。

    Args:
        path (str): 文件的绝对路径 (通常从 list_files 接口获取)。
    """
    try:
        # resolve 后再做 is_relative_to，防止 `../` 之类的路径穿越到 output 之外
        abs_path = Path(path).resolve()
        output_abs = output_dir.resolve()

        if not abs_path.is_relative_to(output_abs):
            return {"error": "拒绝访问: 只能下载输出目录下的文件"}
    except Exception:
        return {"error": "无效的路径参数"}

    if not abs_path.exists():
        return {"error": "文件不存在"}

    # FileResponse 会以流式响应返回文件内容，并让浏览器使用原文件名下载
    return FileResponse(abs_path, filename=abs_path.name)


@app.get("/api/files")
async def list_files(path: str):
    """
    文件列表查询接口 (File Explorer)。

    目标：
    1. 列出指定目录下的所有生成文件。
    2. 提供文件元数据（大小、修改时间、下载所需路径）。
    3. 严格的安全检查，防止路径遍历攻击。

    Args:
        path (str): 目标目录的绝对路径 (必须在 output 目录下)。
    """
    print(f"[DEBUG] 请求文件列表: {path}")

    try:
        # 和下载接口保持同一条安全边界：前端只能查看 output 目录内部内容
        abs_path = Path(path).resolve()
        output_abs = output_dir.resolve()

        if not abs_path.is_relative_to(output_abs):
            print(f"[ERROR] 拒绝访问: {abs_path} 不在 {output_abs} 目录下")
            return {"error": "拒绝访问: 只能访问输出目录下的文件"}

    except Exception as e:
        print(f"[ERROR] 路径解析失败: {e}")
        return {"error": f"路径无效: {e}"}

    if not abs_path.exists():
        return {"error": "目录不存在"}

    files = []
    try:
        # 递归返回文件元数据，前端据此渲染文件列表并发起下载请求
        for file_path in abs_path.rglob("*"):
            if file_path.is_file():
                stat = file_path.stat()
                files.append(
                    {
                        "name": file_path.name,
                        "type": "file",
                        "path": str(file_path),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )

    except Exception as e:
        print(f"[ERROR] 遍历文件失败: {e}")
        return {"error": str(e)}

    # 最新生成的文件排在前面，方便用户优先看到本次任务产物
    files.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    print(f"[DEBUG] 找到 {len(files)} 个文件")
    return {"files": files}


@app.websocket("/ws/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str):
    """
    WebSocket 实时通讯核心接口 (Real-time Communication)。

    连接建立后，ConnectionManager 会用 thread_id 保存 WebSocket。monitor 后续
    发送事件时只需要按 thread_id 查找连接，就能把进度推给对应页面。循环中的
    receive_text 用于接收前端心跳，避免连接空闲断开。
    """
    print(f"会话向我们发起了请求，要求建立连接：{thread_id} 对应：{websocket}")

    # 连接建立后立即按 thread_id 注册，monitor 后续才能把事件定向推给当前页面
    await manager.connect(websocket, thread_id)

    try:
        while True:
            # subscribe{run_id, after_seq} 先 replay durable journal，再进入 live tail
            data = await websocket.receive_text()
            subscribed = await _maybe_replay_subscribe(websocket, thread_id, data)
            if subscribed:
                continue
            await websocket.send_json(
                {"type": "pong", "message": f"服务端已收到: {data}"}
            )

    except WebSocketDisconnect:
        # 只移除当前 WebSocket 实例，避免旧连接断开时误删同 thread_id 的新连接
        manager.disconnect(websocket, thread_id)
        print(f"[WebSocket] 客户端已断开: {thread_id}")

    except Exception as e:
        print(f"[WebSocket] 连接异常: {e}")
        manager.disconnect(websocket, thread_id)


async def _maybe_replay_subscribe(
    websocket: WebSocket, thread_id: str, raw: str
) -> bool:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict) or payload.get("type") != "subscribe":
        return False
    run_id = str(payload.get("run_id") or "")
    try:
        after_seq = int(payload.get("after_seq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    if not run_id:
        latest = get_run_store().latest_run(thread_id)
        run_id = latest.run_id if latest else ""
    events = load_wire_events(
        thread_id,
        run_id=run_id or None,
        after_seq=after_seq,
        limit=2000,
        replay=True,
    )
    for event in events:
        await websocket.send_json(event)
    await websocket.send_json(
        {
            "type": "replay_complete",
            "run_id": run_id,
            "after_seq": after_seq,
            "count": len(events),
        }
    )
    return True


if __name__ == "__main__":
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
