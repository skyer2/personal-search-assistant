import { LinkOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Space, Tabs, Tag, Typography } from "antd";
import { useCallback, useEffect, useState } from "react";
import {
  fetchCitations,
  fetchJsonlTrace,
  fetchLangfuseConfig,
  fetchLangfuseTraces
} from "../lib/api";
import type { EvidenceSource, JsonlTraceEvent, TraceSpanNode, TraceSummary, TraceTree } from "../types";
import { ResizableTable } from "./ResizableTable";

interface TraceViewerProps {
  sessionId: string;
}

function statusColor(status: unknown): string {
  const value = String(status || "").toLowerCase();
  if (["ok", "pass", "enough", "success", "done"].includes(value)) {
    return "green";
  }
  if (["failed", "error", "fail", "abort", "rejected"].includes(value)) {
    return "red";
  }
  if (["gap", "warning", "start", "run"].includes(value)) {
    return value === "gap" || value === "warning" ? "orange" : "blue";
  }
  return "default";
}

function asText(value: unknown, fallback = "-"): string {
  if (value == null || value === "") {
    return fallback;
  }
  if (Array.isArray(value)) {
    return value.length ? value.map((item) => String(item)).join(", ") : fallback;
  }
  return String(value);
}

function SpanTree({ nodes }: { nodes: TraceSpanNode[] }) {
  if (!nodes.length) {
    return <Typography.Text type="secondary">暂无 span 树，先完成一次 Harness run</Typography.Text>;
  }
  return (
    <ol className="trace-span-tree">
      {nodes.map((node) => (
        <li key={node.span_id}>
          <div className="trace-span-node">
            <strong>{node.name}</strong>
            {node.task_id ? <Tag>{node.task_id}</Tag> : null}
            {node.status ? <Tag color={node.status === "failed" || node.status === "error" ? "red" : "blue"}>{node.status}</Tag> : null}
            {typeof node.duration_ms === "number" ? <span>{node.duration_ms}ms</span> : null}
            {typeof node.plan_version === "number" ? <span>plan v{node.plan_version}</span> : null}
          </div>
          {node.children?.length ? <SpanTree nodes={node.children} /> : null}
        </li>
      ))}
    </ol>
  );
}

export function TraceViewer({ sessionId }: TraceViewerProps) {
  const [jsonlEvents, setJsonlEvents] = useState<JsonlTraceEvent[]>([]);
  const [traceTree, setTraceTree] = useState<TraceTree>({ roots: [], span_count: 0, event_count: 0 });
  const [summary, setSummary] = useState<TraceSummary>({});
  const [citations, setCitations] = useState<EvidenceSource[]>([]);
  const [highlightSourceId, setHighlightSourceId] = useState<string | null>(null);
  const [langfuseEnabled, setLangfuseEnabled] = useState(false);
  const [langfuseUrl, setLangfuseUrl] = useState<string | null>(null);
  const [jsonlMessage, setJsonlMessage] = useState("");
  const [citationsMessage, setCitationsMessage] = useState("");
  const [langfuseMessage, setLangfuseMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!sessionId) {
      return;
    }
    setLoading(true);
    setError("");
    try {
      const [jsonl, citationsResp, lfConfig] = await Promise.all([
        fetchJsonlTrace(sessionId).catch((err: unknown) => ({
          events: [],
          message: err instanceof Error ? err.message : "JSONL 加载失败",
          tree: undefined
        })),
        fetchCitations(sessionId).catch((err: unknown) => ({
          sources: [],
          message: err instanceof Error ? err.message : "证据链加载失败"
        })),
        fetchLangfuseConfig().catch(() => ({
          enabled: false,
          host: "",
          ui_url: null
        }))
      ]);
      const lfTraces = lfConfig.enabled
        ? await fetchLangfuseTraces(sessionId).catch(() => ({
            enabled: false,
            traces: [],
            message: "Langfuse 请求失败，已回退本地因果树"
          }))
        : {
            enabled: false,
            traces: [],
            message: "Langfuse 未配置，已跳过"
          };
      setJsonlEvents(jsonl.events || []);
      setJsonlMessage(jsonl.message || "");
      setTraceTree(
        jsonl.tree ||
          (lfTraces as { tree?: TraceTree }).tree || { roots: [], span_count: 0, event_count: 0 }
      );
      setSummary("summary" in jsonl && jsonl.summary ? jsonl.summary : {});
      setCitations(citationsResp.sources || []);
      setCitationsMessage(citationsResp.message || "");
      setLangfuseEnabled(Boolean(lfConfig.enabled));
      setLangfuseUrl(lfConfig.enabled ? lfConfig.ui_url || lfConfig.host || null : null);
      setLangfuseMessage(lfTraces.message || "");
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载 Trace 失败");
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  function handleCitationClick(source: EvidenceSource) {
    setHighlightSourceId(source.source_id);
  }

  const highlightedSteps = new Set(
    citations
      .filter((item) => item.source_id === highlightSourceId)
      .map((item) => item.step_index)
  );
  const workers = summary.workers || [];
  const progress = summary.progress || [];
  const replans = summary.replans || [];
  const evals = summary.evals || [];
  const progressCount = summary.progress_count ?? progress.length;
  const replanCount = summary.replan_count ?? 0;

  return (
    <div className="trace-viewer">
      <div className="panel-heading-row">
        <div>
          <span className="panel-kicker">OBSERVABILITY</span>
          <Typography.Title level={4}>Trace 查看器</Typography.Title>
          <Typography.Text type="secondary">
            session={summary.identity?.session_id || sessionId}
            {summary.identity?.run_id ? ` · run=${summary.identity.run_id}` : ""}
            {summary.identity?.trace_id ? ` · trace=${String(summary.identity.trace_id).slice(0, 12)}` : ""}
          </Typography.Text>
        </div>
        <Button icon={<ReloadOutlined aria-hidden />} loading={loading} onClick={() => void load()}>
          刷新
        </Button>
      </div>

      {error ? <Alert message={error} showIcon type="error" /> : null}

      <Tabs
        items={[
          {
            key: "tree",
            label: `因果树 (${traceTree.span_count})`,
            children: (
              <Card size="small">
                {typeof traceTree.omitted_count === "number" && traceTree.omitted_count > 0 ? (
                  <Alert
                    message={`已从因果树省略 ${traceTree.omitted_count} 条 llm_usage / gen_ai.chat，完整序列见 JSONL 页签。`}
                    showIcon
                    type="info"
                  />
                ) : null}
                <SpanTree nodes={traceTree.roots || []} />
              </Card>
            )
          },
          {
            key: "workers",
            label: `Worker (${summary.worker_count || workers.length})`,
            children: (
              <Card size="small">
                {workers.length === 0 ? (
                  <Alert message="本 run 尚未写入 worker.started / worker.completed" showIcon type="info" />
                ) : (
                  <ResizableTable
                    dataSource={workers.map((row, index) => ({ ...row, key: `${String(row.task_id || "w")}-${String(row.attempt || index)}` }))}
                    pagination={{ pageSize: 12 }}
                    size="small"
                    columns={[
                      { title: "Task", dataIndex: "task_id", width: 180, key: "task_id" },
                      {
                        title: "Status",
                        dataIndex: "status",
                        width: 90,
                        key: "status",
                        render: (status: unknown) => <Tag color={statusColor(status)}>{asText(status)}</Tag>
                      },
                      { title: "ms", dataIndex: "duration_ms", width: 100, key: "duration_ms" },
                      { title: "Attempt", dataIndex: "attempt", width: 90, key: "attempt" },
                      {
                        title: "Plan",
                        dataIndex: "plan_version",
                        width: 80,
                        key: "plan_version",
                        render: (version: unknown) => (version == null || version === "" ? "-" : `v${version}`)
                      },
                      {
                        title: "Objective",
                        dataIndex: "objective",
                        width: 420,
                        key: "objective",
                        render: (value: unknown) => <div className="table-wrap-cell">{asText(value)}</div>
                      },
                      {
                        title: "Fail",
                        dataIndex: "fail_reason",
                        width: 180,
                        key: "fail_reason",
                        render: (value: unknown) => <div className="table-wrap-cell">{asText(value, "")}</div>
                      }
                    ]}
                  />
                )}
              </Card>
            )
          },
          {
            key: "replan",
            label: `进度 (${progressCount}) / Replan (${replanCount})`,
            children: (
              <Card size="small">
                {progress.length === 0 && replans.length === 0 ? (
                  <Alert
                    message="尚未写入 progress.evaluated。第二波 Worker 可能只是计划内 READY 任务按 max_parallel 分批执行，不一定经过 PlanPatch。"
                    showIcon
                    type="info"
                  />
                ) : null}
                {progress.length > 0 && replanCount === 0 ? (
                  <Alert
                    message="进度已评估但未应用 PlanPatch。verdict=enough，或 gap 但被 max_replan / validator 拦住时，后续 Worker 仍是原计划 READY 队列。"
                    showIcon
                    type="info"
                  />
                ) : null}
                {progress.length > 0 ? (
                  <>
                    <Typography.Title level={5}>进度评估</Typography.Title>
                    <ResizableTable
                      dataSource={progress.map((row, index) => ({ ...row, key: `p-${index}` }))}
                      pagination={false}
                      size="small"
                      columns={[
                        {
                          title: "Verdict",
                          dataIndex: "verdict",
                          width: 110,
                          key: "verdict",
                          render: (verdict: unknown) => <Tag color={statusColor(verdict)}>{asText(verdict)}</Tag>
                        },
                        {
                          title: "Plan",
                          dataIndex: "plan_version",
                          width: 80,
                          key: "plan_version",
                          render: (version: unknown) => (version == null || version === "" ? "-" : `v${version}`)
                        },
                        {
                          title: "Reason",
                          dataIndex: "reason",
                          width: 280,
                          key: "reason",
                          render: (value: unknown) => <div className="table-wrap-cell">{asText(value)}</div>
                        },
                        {
                          title: "Gaps",
                          width: 280,
                          key: "gaps",
                          render: (_, row) => <div className="table-wrap-cell">{asText(row.gaps)}</div>
                        },
                        {
                          title: "Conflicts",
                          dataIndex: "conflict_count",
                          width: 100,
                          key: "conflict_count",
                          render: (value: unknown) => asText(value, "0")
                        }
                      ]}
                    />
                  </>
                ) : null}
                <Typography.Title level={5}>Replan</Typography.Title>
                {replans.length === 0 ? (
                  <Typography.Text type="secondary">没有 replan.applied / replan.failed 事件。</Typography.Text>
                ) : (
                  <ResizableTable
                    dataSource={replans.map((row, index) => ({ ...row, key: `r-${index}` }))}
                    pagination={false}
                    size="small"
                    columns={[
                      { title: "Event", dataIndex: "type", width: 150, key: "type" },
                      {
                        title: "Version",
                        width: 140,
                        key: "version",
                        render: (_, row) => `${asText(row.from_plan_version)} → ${asText(row.to_plan_version)}`
                      },
                      {
                        title: "Reason",
                        dataIndex: "reason",
                        width: 280,
                        key: "reason",
                        render: (value: unknown) => <div className="table-wrap-cell">{asText(value)}</div>
                      },
                      {
                        title: "Added",
                        width: 240,
                        key: "added_tasks",
                        render: (_, row) => <div className="table-wrap-cell">{asText(row.added_tasks)}</div>
                      }
                    ]}
                  />
                )}
              </Card>
            )
          },
          {
            key: "eval",
            label: `Eval (${evals.length})`,
            children: (
              <Card size="small">
                {summary.usage ? (
                  <Alert
                    message={`LLM ${summary.usage.calls || 0} calls · tokens ${summary.usage.total_tokens || 0} · cost $${Number(summary.usage.cost_usd || 0).toFixed(4)}`}
                    showIcon
                    type="info"
                  />
                ) : null}
                {evals.length === 0 ? (
                  <Alert
                    message="交互提问不会产生 eval.scored（那是 tests/eval/run_eval.py --live）。Finalize 后应出现 quality.evaluated；若仍为空，说明质量评估尚未发出或 run 未结束。"
                    showIcon
                    type="info"
                  />
                ) : (
                  <ResizableTable
                    dataSource={evals.map((row, index) => ({ ...row, key: `e-${index}` }))}
                    pagination={false}
                    size="small"
                    columns={[
                      { title: "Event", dataIndex: "type", width: 150, key: "type" },
                      {
                        title: "Status",
                        dataIndex: "status",
                        width: 100,
                        key: "status",
                        render: (status: unknown) => <Tag color={statusColor(status)}>{asText(status)}</Tag>
                      },
                      { title: "Case", dataIndex: "case_id", width: 160, key: "case_id", render: (value: unknown) => asText(value) },
                      { title: "Variant", dataIndex: "variant", width: 140, key: "variant", render: (value: unknown) => asText(value) },
                      { title: "Accuracy", dataIndex: "accuracy", width: 110, key: "accuracy", render: (value: unknown) => asText(value) },
                      { title: "Citation", dataIndex: "citation_score", width: 110, key: "citation_score", render: (value: unknown) => asText(value) },
                      {
                        title: "Passed",
                        dataIndex: "passed",
                        width: 90,
                        key: "passed",
                        render: (value: unknown) => (value == null ? "-" : String(value))
                      },
                      { title: "ms", dataIndex: "latency_ms", width: 100, key: "latency_ms", render: (value: unknown) => asText(value) }
                    ]}
                  />
                )}
              </Card>
            )
          },
          {
            key: "jsonl",
            label: `JSONL (${jsonlEvents.length})`,
            children: (
              <Card size="small">
                {jsonlMessage ? <Alert message={jsonlMessage} showIcon type="info" /> : null}
                <ResizableTable
                  dataSource={jsonlEvents.map((event, index) => ({ ...event, key: `${event.phase}-${index}` }))}
                  pagination={{ pageSize: 12 }}
                  size="small"
                  rowClassName={(row) =>
                    typeof row.step_index === "number" && highlightedSteps.has(row.step_index)
                      ? "trace-row-highlight"
                      : ""
                  }
                  columns={[
                    {
                      title: "Event",
                      key: "event",
                      render: (_, row) => String(row.type || row.event || row.phase || "-"),
                      width: 180
                    },
                    { title: "Phase", dataIndex: "phase", width: 120, key: "phase" },
                    { title: "Status", dataIndex: "status", width: 100, key: "status" },
                    { title: "Task", dataIndex: "task_id", width: 120, key: "task_id" },
                    {
                      title: "Step",
                      width: 80,
                      key: "step",
                      render: (_, row) => (typeof row.step_index === "number" ? row.step_index + 1 : "-")
                    },
                    { title: "ms", dataIndex: "duration_ms", width: 90, key: "duration_ms" },
                    {
                      title: "Time",
                      dataIndex: "timestamp",
                      width: 220,
                      key: "timestamp",
                      render: (value: unknown) => <div className="table-wrap-cell">{asText(value)}</div>
                    }
                  ]}
                />
              </Card>
            )
          },
          {
            key: "citations",
            label: `证据链 (${citations.length})`,
            children: (
              <Card size="small">
                {citationsMessage ? <Alert message={citationsMessage} showIcon type="info" /> : null}
                <ResizableTable
                  dataSource={citations.map((source, index) => ({
                    ...source,
                    key: source.source_id || `cite-${index}`,
                    ref_num: index + 1
                  }))}
                  pagination={{ pageSize: 10 }}
                  size="small"
                  columns={[
                    {
                      title: "引用",
                      dataIndex: "ref_num",
                      width: 72,
                      key: "ref_num",
                      render: (num: number) => <Tag color="blue">[{num}]</Tag>
                    },
                    {
                      title: "类型",
                      dataIndex: "source_kind",
                      width: 90,
                      key: "source_kind",
                      render: (kind: string) => <Tag>{kind}</Tag>
                    },
                    {
                      title: "Step",
                      width: 160,
                      key: "step",
                      render: (_, row) => `${row.step_index + 1} / ${row.step_type}`
                    },
                    {
                      title: "来源",
                      dataIndex: "locator",
                      width: 360,
                      key: "locator",
                      render: (locator: string, row) =>
                        row.source_kind === "url" ? (
                          <a href={locator} rel="noreferrer" target="_blank">
                            {locator}
                          </a>
                        ) : (
                          <div className="table-wrap-cell">{locator}</div>
                        )
                    },
                    {
                      title: "操作",
                      width: 110,
                      key: "actions",
                      render: (_, row) => (
                        <Button size="small" type="link" onClick={() => handleCitationClick(row)}>
                          高亮 Step
                        </Button>
                      )
                    }
                  ]}
                />
                {highlightSourceId ? (
                  <Alert
                    className="trace-citation-hint"
                    message={`已高亮 source_id=${highlightSourceId} 对应 execute 步骤，请查看 JSONL 页签`}
                    showIcon
                    type="success"
                  />
                ) : null}
              </Card>
            )
          },
          {
            key: "langfuse",
            label: "Langfuse / OTLP",
            children: (
              <Card size="small">
                {langfuseEnabled && langfuseUrl ? (
                  <Space>
                    <Typography.Text>通过 OpenTelemetry OTLP 导出到 Langfuse（不再调用 /api/public/traces）</Typography.Text>
                    <a href={langfuseUrl} rel="noreferrer" target="_blank">
                      <LinkOutlined aria-hidden /> 打开 Langfuse UI
                    </a>
                  </Space>
                ) : (
                  <Alert
                    message={langfuseMessage || "Langfuse 未配置；本地因果树仍可用"}
                    showIcon
                    type="warning"
                  />
                )}
                <SpanTree nodes={traceTree.roots || []} />
              </Card>
            )
          }
        ]}
      />
    </div>
  );
}
