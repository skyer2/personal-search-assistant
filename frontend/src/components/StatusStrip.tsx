import {
  ApiOutlined,
  BranchesOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  FileDoneOutlined,
  ToolOutlined
} from "@ant-design/icons";
import type { ConnectionState } from "../types";
import type { RunStatsProjection } from "../lib/runStats";

interface StatusStripProps {
  connectionState: ConnectionState;
  isRunning: boolean;
  stats: RunStatsProjection;
}

function connectionLabel(state: ConnectionState): string {
  const labels: Record<ConnectionState, string> = {
    connecting: "连接中",
    connected: "已连接",
    reconnecting: "重连中",
    closed: "已关闭"
  };
  return labels[state];
}

export function StatusStrip({ connectionState, isRunning, stats }: StatusStripProps) {
  const online = connectionState === "connected";

  return (
    <section className="status-strip" aria-label="任务状态">
      <div className={`metric-tile ${online ? "metric-tile--online" : "metric-tile--warn"}`}>
        <ApiOutlined aria-hidden />
        <div>
          <span>WebSocket</span>
          <strong>{connectionLabel(connectionState)}</strong>
        </div>
      </div>
      <div className={`metric-tile ${isRunning ? "metric-tile--live" : ""}`}>
        {isRunning ? <BranchesOutlined aria-hidden /> : <CheckCircleOutlined aria-hidden />}
        <div>
          <span>任务态</span>
          <strong>{isRunning ? "执行中" : "待命"}</strong>
        </div>
      </div>
      <div className="metric-tile">
        <ToolOutlined aria-hidden />
        <div>
          <span>工具调用</span>
          <strong>{stats.toolCalls}</strong>
        </div>
      </div>
      <div className="metric-tile">
        <BranchesOutlined aria-hidden />
        <div>
          <span>Worker 执行</span>
          <strong>{stats.workerRuns}</strong>
        </div>
      </div>
      <div className="metric-tile">
        <BranchesOutlined aria-hidden />
        <div>
          <span>Worker 成功</span>
          <strong>{stats.workerSucceeded}</strong>
        </div>
      </div>
      <div className="metric-tile">
        <BranchesOutlined aria-hidden />
        <div>
          <span>Worker 部分</span>
          <strong>{stats.workerPartial}</strong>
        </div>
      </div>
      <div className={`metric-tile ${stats.workerFailed > 0 ? "metric-tile--error" : ""}`}>
        <CloseCircleOutlined aria-hidden />
        <div>
          <span>Worker 失败</span>
          <strong>{stats.workerFailed}</strong>
        </div>
      </div>
      <div className={`metric-tile ${stats.toolFailed > 0 ? "metric-tile--error" : ""}`}>
        <CloseCircleOutlined aria-hidden />
        <div>
          <span>工具失败</span>
          <strong>{stats.toolFailed}</strong>
        </div>
      </div>
      <div className="metric-tile">
        <FileDoneOutlined aria-hidden />
        <div>
          <span>产物</span>
          <strong>{stats.fileCount}</strong>
        </div>
      </div>
      <div className={`metric-tile ${stats.runtimeFailed > 0 ? "metric-tile--error" : ""}`}>
        <CloseCircleOutlined aria-hidden />
        <div>
          <span>运行异常</span>
          <strong>{stats.runtimeFailed}</strong>
        </div>
      </div>
      <div className={`metric-tile ${stats.providerFailed > 0 ? "metric-tile--error" : ""}`}>
        <CloseCircleOutlined aria-hidden />
        <div>
          <span>Provider 异常</span>
          <strong>{stats.providerFailed}</strong>
        </div>
      </div>
    </section>
  );
}
