# Research Agent Harness Frontend

React + Vite + Tailwind CSS + Ant Design 实验台，对接 FastAPI Harness。

这不是搜索产品 UI。页面用来提交研究任务、看 StateGraph 执行过程、Eval 和 Trace。

生产交互按任务状态机渲染，而不是「只要没结束就一直转圈」：

| 状态 | UI |
|------|----|
| `idle` | 待命 |
| `running` | 按 Harness Phase 显示确定性进度（0–100%） |
| `awaiting_approval` | HITL 暂停：进度条冻结、计时停止 |
| `cancelling` / `completed` / `failed` | 对应静态结果态 |

HITL 审批卡片会吸顶。原始 WebSocket 事件默认折进「原始事件日志」。

开发时可用 `http://localhost:5173/?preview=run-states` 对照运行中 / 暂停 / 完成三种进度条。

## Run

```bash
pnpm install
pnpm dev
```

默认连接 `http://localhost:8000` 和 `ws://localhost:8000`。
可用 `.env.local` 覆盖：

```bash
VITE_API_BASE_URL=http://localhost:8000
VITE_WS_BASE_URL=ws://localhost:8000
```

## Backend Contract

- `POST /api/task`（默认 `mode=agent`）
- `POST /api/upload`
- `GET /api/files`
- `GET /api/download`
- `POST /api/task/{thread_id}/resume`
- `WebSocket /ws/{thread_id}`（`phase` / `hitl_interrupt` / `task_result`）
