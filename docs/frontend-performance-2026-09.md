# 前台性能放大回路修复记录（2026-09）

## 问题定位

本次前台长时间卡顿不是单个组件慢，而是一个互相放大的回路：

1. `/api/runs/{run_id}/artifacts` 失败后，前端回退调用 Session 级 artifacts，一次性拿到 1686+ 历史文件。
2. `DeliverableFiles` 在消息流中两处全量渲染，形成约 3371 个文件卡片。
3. 顶层 elapsed 计时器每秒触发一次整页 state 更新，所有文件卡片与 Markdown 重新参与渲染。
4. 每秒执行的 `linkifyArtifactNames` 对 1686 个文件名做 `replaceAll`，约 50MB 文本扫描。
5. 文件列表每 2.5 秒轮询，后端同步 `rglob + stat` 又占用请求线程。

结果是主线程持续忙于大列表渲染和文本替换，用户输入与页面滚动被持续阻塞。

## 修改方案

| 优先级 | 问题 | 修复 |
| --- | --- | --- |
| P0 | Run 文件接口失败回退 Session 历史 | 删除 fallback；无 current run 时文件列表为空 |
| P0 | Bootstrap 返回 Session 全历史文件 | 改为 `list_run_output_files(current.run_id)`；无 current run 返回空 |
| P0 | 文件卡片全量渲染 | banner 最多 5 个，shelf 最多 20 个；其余折叠 |
| P0 | `<details>` 收起时仍构造全部行 | 展开后才 `map` 渲染 |
| P0 | 消息内两份文件列表 | 删除 AssistantMessage 底部第二份 artifact block |
| P0 | 文件名 linkify 全量扫描 | 排序后最多处理 20 个文件名 |
| P1 | 每秒顶层 state 更新 | elapsed 计时移入 `ElapsedTimer` 小组件 |
| P1 | Markdown 无谓重渲染 | `MarkdownRenderer` 使用 `memo` |
| P1 | 文件列表轮询 | 改为初始化 + WebSocket 事件触发，`tool_end` 至多 1 秒一次，终态强制刷新 |
| P1 | 相同文件列表触发重渲染 | 按 name/path/size/mtime 浅比较，无变化不 setState |
| P2 | Session 级扫描重复执行 | 5 秒 TTL 缓存，缓存键包含输出目录及一级子项 stat 指纹 |
| P2 | async 端点内同步 IO 阻塞事件循环 | Bootstrap、Session artifacts、Run artifacts 扫描改用 `asyncio.to_thread` |

## 实施细节

- 前端 `ChatTurn` 从 `elapsedMs` 改为持有 `ElapsedClockState`，保留暂停、恢复、终态冻结语义；只有正在计时的小组件内部维持 1 秒 tick。
- `App` 不再依赖每秒变化的 `elapsedMs` 更新 turns，消除了整页每秒 rerender。
- `DeliverableFiles` 的“其余 N 个文件”折叠区使用 `onToggle` 记录展开状态，收起时不渲染隐藏行。
- Run 级 artifacts 不加 TTL，保证终态刷新立即看到新文件；Session 级历史列表加缓存，避免大目录重复扫描。
- 后端文件扫描的缓存返回列表副本，避免调用方修改缓存内部数据。

## 修改文件

- `frontend/src/hooks/useDeepAgentSession.ts`
- `frontend/src/components/DeliverableFiles.tsx`
- `frontend/src/components/ConversationThread.tsx`
- `frontend/src/components/RunProgress.tsx`
- `frontend/src/components/MarkdownRenderer.tsx`
- `frontend/src/lib/sessionProjection.ts`
- `frontend/src/App.tsx`
- `frontend/src/preview/ConversationLayoutPreview.tsx`
- `app/run_store/service.py`
- `app/run_store/files.py`
- `app/api/session_routes.py`
- `tests/test_run_isolation.py`

## 验证

- `node node_modules/typescript/bin/tsc -b --noEmit`：0 errors。
- `pytest tests/test_run_isolation.py tests/test_run_store.py tests/test_durable_session.py`：20 passed。
- 新增回归：Bootstrap `output_files` 只包含当前 Run，且仅有上传、没有 Run 时为空。
