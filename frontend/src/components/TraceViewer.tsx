import { LinkOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Space, Tabs, Tag, Typography } from "antd";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchRunCitations,
  fetchApiMeta,
  fetchLangfuseConfig,
  fetchRunEvents,
  fetchRunLineage,
  fetchRunTraceSummary,
  fetchRunTree,
  fetchSessionTraces
} from "../lib/api";
import type { EvidenceSource, JsonlTraceEvent, SessionTraceItem, TraceSpanNode, TraceSummary, TraceTree } from "../types";
import { formatDurationSeconds, formatObservabilityTime } from "../lib/observabilityFormat";
import { ResizableTable } from "./ResizableTable";

interface TraceViewerProps {
  sessionId: string;
  runId?: string;
}

function statusColor(status: unknown): string {
  const value = String(status || "").toLowerCase();
  if (["ok", "pass", "sufficient", "success", "done"].includes(value)) {
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
    if (!value.length) {
      return fallback;
    }
    return value
      .map((item) => {
        if (item == null) {
          return "";
        }
        if (typeof item === "object") {
          const row = item as Record<string, unknown>;
          const desc = row.description ?? row.gap_id ?? row.type ?? row.reason;
          if (desc != null && desc !== "") {
            const prefix = row.type ? `${String(row.type)}: ` : "";
            return `${prefix}${String(desc)}`;
          }
          try {
            return JSON.stringify(item);
          } catch {
            return "[object]";
          }
        }
        return String(item);
      })
      .filter(Boolean)
      .join(", ");
  }
  if (typeof value === "object") {
    const row = value as Record<string, unknown>;
    if (row.description != null) {
      return String(row.description);
    }
    try {
      return JSON.stringify(value);
    } catch {
      return fallback;
    }
  }
  return String(value);
}

function SpanTree({
  nodes,
  selectedSpanId,
  onSelect
}: {
  nodes: TraceSpanNode[];
  selectedSpanId?: string | null;
  onSelect?: (node: TraceSpanNode) => void;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  if (!nodes.length) {
    return <Typography.Text type="secondary">暂无 span 树，先完成一次 Harness run</Typography.Text>;
  }
  return (
    <ol className="trace-span-tree">
      {nodes.map((node) => (
        <li key={node.span_id}>
          <button
            className={`trace-span-node ${selectedSpanId === node.span_id ? "trace-span-node--selected" : ""}`}
            onClick={() => {
              onSelect?.(node);
              if (node.children?.length) {
                setExpanded((previous) => {
                  const next = new Set(previous);
                  if (next.has(node.span_id)) next.delete(node.span_id);
                  else next.add(node.span_id);
                  return next;
                });
              }
            }}
            style={{
              display: "flex",
              gap: 8,
              alignItems: "center",
              flexWrap: "wrap",
              background: selectedSpanId === node.span_id ? "rgba(22,119,255,0.08)" : "transparent",
              border: "none",
              cursor: "pointer",
              textAlign: "left",
              padding: "4px 0",
              width: "100%"
            }}
            type="button"
          >
            <strong>{node.children?.length ? `${expanded.has(node.span_id) ? "▾" : "▸"} ${node.name}` : node.name}</strong>
            {node.task_id ? <Tag>{node.task_id}</Tag> : null}
            {node.status ? <Tag color={node.status === "failed" || node.status === "error" ? "red" : "blue"}>{node.status}</Tag> : null}
            {node.duration_ms != null ? <span>{formatDurationSeconds(node.duration_ms)}</span> : null}
            {typeof node.plan_version === "number" ? <span>plan v{node.plan_version}</span> : null}
          </button>
          {node.children?.length && expanded.has(node.span_id) ? (
            <SpanTree nodes={node.children} onSelect={onSelect} selectedSpanId={selectedSpanId} />
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function TraceViewerImpl({ sessionId, runId }: TraceViewerProps) {
  const [loadState, setLoadState] = useState<"not_loaded" | "loading" | "loaded" | "error">("not_loaded");
  const [releaseMismatch, setReleaseMismatch] = useState("");
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
  const [traces, setTraces] = useState<SessionTraceItem[]>([]);
  const [selectedRunId, setSelectedRunId] = useState(runId || "");
  const [selectedSpan, setSelectedSpan] = useState<TraceSpanNode | null>(null);
  const [activeTab, setActiveTab] = useState("overview");
  const [loadedTabs, setLoadedTabs] = useState<Set<string>>(() => new Set());
  const [lineageTotal, setLineageTotal] = useState(0);
  const [citationsTotal, setCitationsTotal] = useState(0);
  const requestGeneration = useRef(0);

  useEffect(() => {
    if (runId) {
      setSelectedRunId(runId);
    }
  }, [runId]);

  const load = useCallback(async () => {
    if (!sessionId) {
      return;
    }
    const generation = ++requestGeneration.current;
    setLoading(true);
    setLoadState("loading");
    setError("");
    setJsonlEvents([]);
    setTraceTree({ roots: [], span_count: 0, event_count: 0 });
    setCitations([]);
    setSelectedSpan(null);
    setLineageTotal(0);
    setCitationsTotal(0);
    setLoadedTabs(new Set());
    try {
      const listed = await fetchSessionTraces(sessionId).catch(() => ({ traces: [] as SessionTraceItem[], current_run_id: "" }));
      const nextTraces = listed.traces || [];
      setTraces(nextTraces);
      const activeRun = selectedRunId || runId || listed.current_run_id || nextTraces[nextTraces.length - 1]?.run_id || "";
      if (activeRun && activeRun !== selectedRunId) {
        setSelectedRunId(activeRun);
      }
      const [jsonl, lfConfig, apiMetaResult] = await Promise.all([
        activeRun ? fetchRunTraceSummary(activeRun) : Promise.resolve({ summary: {}, total: 0, session_id: sessionId }),
        fetchLangfuseConfig().catch(() => ({
          enabled: false,
          host: "",
          ui_url: null
        })),
        // Meta 是版本兼容提示，不是 Trace 渲染的硬依赖：404 时降级为 unknown
        fetchApiMeta().catch(() => ({
          git_sha: "unknown",
          api_schema: "unknown",
          event_schema: "unknown",
          config_hash: "unknown",
          started_at: "",
          environment: "unknown"
        }))
      ]);
      const apiMeta = apiMetaResult;
      if (generation !== requestGeneration.current) return;
      const nextSummary: TraceSummary = "summary" in jsonl && jsonl.summary ? jsonl.summary : {};
      setSummary(nextSummary);
      setLineageTotal(Number(nextSummary.counts?.lineage || 0));
      setCitationsTotal(Number(nextSummary.counts?.evidence || 0));
      setLangfuseEnabled(Boolean(lfConfig.enabled));
      setLangfuseUrl(lfConfig.enabled ? lfConfig.ui_url || lfConfig.host || null : null);
      setLangfuseMessage(lfConfig.enabled ? "" : "Langfuse 未配置，已跳过");
      const expectedSchema = import.meta.env.VITE_API_SCHEMA || "research-api.v3";
      const uiSha = import.meta.env.VITE_GIT_SHA || "unknown";
      const shaMismatch = uiSha !== "unknown" && apiMeta.git_sha !== "unknown" && !apiMeta.git_sha.startsWith(uiSha) && !uiSha.startsWith(apiMeta.git_sha);
      const schemaMismatch = apiMeta.api_schema !== expectedSchema;
      setReleaseMismatch(schemaMismatch || shaMismatch
        ? `Frontend/Backend version mismatch · UI ${uiSha} (${expectedSchema}) · API ${apiMeta.git_sha.slice(0, 8)} (${apiMeta.api_schema})`
        : "");
      setLoadState("loaded");
    } catch (err) {
      const message = err instanceof Error ? err.message : "加载 Trace 失败";
      setError(`Trace data unavailable · ${message} · 无法判断 Brief / Plan / Worker / Synthesis 状态`);
      setLoadState("error");
    } finally {
      setLoading(false);
    }
  }, [runId, selectedRunId, sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadTab = useCallback(async (key: string) => {
    if (!selectedRunId || loadedTabs.has(key)) return;
    const generation = requestGeneration.current;
    if (key === "jsonl") {
      const response = await fetchRunEvents(selectedRunId, { limit: 100 });
      if (generation !== requestGeneration.current) return;
      setJsonlEvents((response.events || []) as unknown as JsonlTraceEvent[]);
    } else if (key === "lineage") {
      const response = await fetchRunLineage(selectedRunId, 0, 100);
      if (generation !== requestGeneration.current) return;
      setSummary((previous) => ({ ...previous, lineage: response.items }));
      setLineageTotal(response.total);
    } else if (key === "tree") {
      const [treeResponse, eventResponse] = await Promise.all([
        fetchRunTree(selectedRunId),
        fetchRunEvents(selectedRunId, { limit: 100 })
      ]);
      if (generation !== requestGeneration.current) return;
      setTraceTree(treeResponse.tree);
      setJsonlEvents((eventResponse.events || []) as unknown as JsonlTraceEvent[]);
    } else if (key === "langfuse") {
      const response = await fetchRunTree(selectedRunId);
      if (generation !== requestGeneration.current) return;
      setTraceTree(response.tree);
    } else if (key === "citations") {
      const response = await fetchRunCitations(selectedRunId);
      if (generation !== requestGeneration.current) return;
      setCitations(response.sources || []);
      setCitationsTotal(response.total || 0);
      setCitationsMessage(response.message || "");
    }
    setLoadedTabs((previous) => new Set(previous).add(key));
  }, [loadedTabs, selectedRunId]);

  function handleCitationClick(source: EvidenceSource) {
    setHighlightSourceId(source.source_id);
  }

  const highlightedSteps = useMemo(() => new Set(
    citations
      .filter((item) => item.source_id === highlightSourceId)
      .map((item) => item.step_index)
  ), [citations, highlightSourceId]);
  const workers = summary.workers || [];
  const topology = summary.topology || null;
  const supervisorDecisions = summary.supervisor_decisions || [];
  const findings = summary.findings || [];
  const coverageJudgements = summary.coverage_judgements || [];
  const progress = summary.progress || [];
  const evals = summary.evals || [];
  const plans = summary.plans || [];
  const synthesis = summary.synthesis || [];
  const claimEvidenceBindings = useMemo(() => synthesis.flatMap((row) => {
    const bindings = (row as Record<string, unknown>).claim_evidence_bindings;
    return Array.isArray(bindings)
      ? bindings.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
      : [];
  }), [synthesis]);
  const lineage = summary.lineage || [];
  const brief = summary.brief || null;
  const failureOrigin = summary.failure_origin || null;
  const integrity = summary.trace_integrity || null;
  const progressCount = summary.progress_count ?? progress.length;
  const latency = summary.latency || null;
  const latencyRows = latency
      ? [
        ["Total", latency.total_ms],
        ["Time to first evidence", latency.time_to_first_evidence_ms],
        ["Time to enough evidence", latency.time_to_enough_evidence_ms],
        ["Intent router", latency.intent_router_ms ?? latency.stage_ms?.intent_router],
        ["Understanding", latency.understand_ms],
        ["Brief + plan", latency.brief_plan_ms ?? latency.stage_ms?.brief_plan],
        ["Topology", latency.topology_ms],
        ["Planning", latency.planning_ms],
        ["Plan validation", latency.plan_validate_ms ?? latency.stage_ms?.plan_validate],
        ["Research wall", latency.research_wall_ms],
        ["Parallel saved", latency.research_parallel_saved_ms],
        ["Gap check", latency.gap_check_ms],
        ["Gap precheck", latency.gap_precheck_ms ?? latency.stage_ms?.gap_precheck],
        ["Supervisor", Array.isArray(latency.supervisor_ms) ? latency.supervisor_ms.reduce((sum, value) => sum + Number(value || 0), 0) : undefined],
        ["Synthesis", latency.synthesis_ms],
        ["Quality gate", latency.quality_blocking_ms],
        ["Time to answer", latency.time_to_final_answer_ms],
        ["Delivery", latency.delivery_ms],
        ["Post-run", latency.post_run_ms],
        ["LLM", latency.llm_ms],
        ["Tools", latency.tool_ms],
        ["Storage", latency.storage_ms],
        ["Telemetry", latency.telemetry_ms],
        ["Control-plane ratio", latency.control_plane_ratio == null ? undefined : `${(Number(latency.control_plane_ratio) * 100).toFixed(1)}%`],
        ["Worker LLM share", latency.worker_llm_ratio == null ? undefined : `${(Number(latency.worker_llm_ratio) * 100).toFixed(1)}%`],
        ["Worker tool share", latency.worker_tool_ratio == null ? undefined : `${(Number(latency.worker_tool_ratio) * 100).toFixed(1)}%`],
        ["Worker idle share", latency.worker_idle_ratio == null ? undefined : `${(Number(latency.worker_idle_ratio) * 100).toFixed(1)}%`]
      ].map(([label, value], index) => ({
        key: `${String(label)}-${index}`,
        metric: String(label),
        value,
        ratio: String(label).startsWith("Worker ") || String(label) === "Control-plane ratio"
      }))
    : [];

  return (
    <div className="trace-viewer">
      <div className="panel-heading-row">
        <div>
          <span className="panel-kicker">OBSERVABILITY</span>
          <Typography.Title level={4}>Trace 查看器</Typography.Title>
          <Typography.Text type="secondary">
            session={summary.identity?.session_id || sessionId}
            {summary.identity?.run_id || selectedRunId ? ` · run=${summary.identity?.run_id || selectedRunId}` : ""}
            {summary.identity?.trace_id ? ` · trace=${String(summary.identity.trace_id).slice(0, 12)}` : ""}
            {summary.identity?.git_sha ? ` · build=${String(summary.identity.git_sha).slice(0, 8)}` : ""}
            {summary.identity?.config_hash ? ` · config=${String(summary.identity.config_hash).slice(0, 8)}` : ""}
          </Typography.Text>
        </div>
        <Space>
          {traces.length > 0 ? (
            <select
              aria-label="选择 run"
              className="trace-run-select"
              onChange={(event) => setSelectedRunId(event.target.value)}
              value={selectedRunId || traces[traces.length - 1]?.run_id || ""}
            >
              {traces.map((item) => (
                <option key={item.run_id} value={item.run_id}>
                  {(item.run_id || "").slice(0, 8)} · {item.status || "run"}
                </option>
              ))}
            </select>
          ) : null}
          <Button icon={<ReloadOutlined aria-hidden />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
        </Space>
      </div>

      {releaseMismatch ? <Alert message={releaseMismatch} showIcon type="error" style={{ marginBottom: 12 }} /> : null}
      {error ? <Alert message={error} showIcon type="error" /> : null}

      {/* 语义流水线 → 产物证据 → 运行时/调试：
          Overview → Understanding → Strategy → Worker → Supervisor/Coverage → Synthesis →
          Evidence → Lineage → Span Tree → Run Eval → JSONL → Langfuse */}
      <Tabs
        activeKey={activeTab}
        destroyInactiveTabPane
        onChange={(key) => {
          setActiveTab(key);
          void loadTab(key);
        }}
        items={[
          {
            key: "overview",
            label: "Overview",
            children: (
              <Card size="small">
                {integrity ? (
                  <Alert
                    message={
                      integrity.passed
                        ? `Trace Integrity: PASS — ${Object.entries(integrity.counts || {}).map(([k, v]) => `${k}=${v}`).join(" · ")}`
                        : `Trace Integrity: FAIL — ${(integrity.issues || []).join(", ")}`
                    }
                    showIcon
                    style={{ marginBottom: 12 }}
                    type={integrity.passed ? "success" : "error"}
                  />
                ) : null}
                {integrity?.span_tree && integrity.span_tree.valid !== null && integrity.span_tree.valid !== undefined ? (
                  <Typography.Paragraph type="secondary">
                    Span tree: {integrity.span_tree.span_count} spans / {integrity.span_tree.root_count} roots / {integrity.span_tree.cycle_count} cycles
                    {integrity.span_tree.valid ? " ✓" : " ✗"}
                  </Typography.Paragraph>
                ) : null}
                {failureOrigin ? (
                  <Alert
                    message={`Earliest failure: ${asText(failureOrigin.origin_stage)} → detected@${asText(failureOrigin.detected_stage)} (${asText(failureOrigin.type)})`}
                    showIcon
                    style={{ marginBottom: 12 }}
                    type="warning"
                  />
                ) : summary.status === "partial" ? (
                  <Alert message={`部分结束：${asText(summary.termination?.reason, "termination reason missing")}`} showIcon style={{ marginBottom: 12 }} type="warning" />
                ) : (
                  <Alert message="本 run 暂无语义 failure origin" showIcon style={{ marginBottom: 12 }} type="success" />
                )}
                <Typography.Paragraph>
                  Topology: {asText(topology?.topology, "unknown")}
                  {" · "}
                  Supervisor decisions: {supervisorDecisions.length}
                  {" · "}
                  Findings: {findings.length}
                  {" · "}
                  Coverage: {coverageJudgements.length ? asText(coverageJudgements[coverageJudgements.length - 1].status, "unknown") : "not assessed"}
                  {" · "}
                  Lineage edges: {lineageTotal}
                </Typography.Paragraph>
                {latency ? (
                  <Card size="small" title="Latency Breakdown" style={{ marginBottom: 12 }}>
                    <ResizableTable
                      dataSource={latencyRows}
                      pagination={false}
                      size="small"
                      columns={[
                        { title: "Metric", dataIndex: "metric", key: "metric", width: 180 },
                        {
                          title: "Time",
                          dataIndex: "value",
                          key: "value",
                          render: (value: unknown, row: { ratio?: boolean }) => row.ratio ? asText(value) : formatDurationSeconds(value)
                        }
                      ]}
                    />
                  </Card>
                ) : null}
                {brief ? (
                  <Typography.Paragraph>
                    Brief {asText(brief.brief_id)} · dims={(brief.dimensions as string[] | undefined)?.join(", ") || "-"}
                  </Typography.Paragraph>
                ) : (
                  <Alert message={loadState === "loaded" ? "已加载：未写入 brief.compiled" : "Trace 未加载，Brief 状态未知"} showIcon type="info" />
                )}
              </Card>
            )
          },
          {
            key: "understand",
            label: "Understanding",
            children: (
              <Card size="small">
                {!brief ? (
                  <Alert message={loadState === "loaded" ? "已加载：未写入 ResearchBrief 投影（brief.compiled）" : "Trace 未加载，ResearchBrief 状态未知"} showIcon type="info" />
                ) : (
                  <ResizableTable
                    dataSource={[
                      { key: "objective", field: "objective", value: asText(brief.objective) },
                      { key: "entities", field: "entities", value: asText((brief.entities as string[] | undefined)?.join(", ")) },
                      { key: "dimensions", field: "dimensions", value: asText((brief.dimensions as string[] | undefined)?.join(", ")) },
                      { key: "depth", field: "depth", value: asText(brief.depth) },
                      { key: "freshness", field: "freshness", value: asText(brief.freshness) },
                      { key: "deliverable", field: "deliverable", value: asText(brief.deliverable) },
                      { key: "brief_ref", field: "brief_ref", value: asText(brief.brief_ref) },
                      { key: "topology", field: "topology", value: asText(topology?.topology) },
                      {
                        key: "topology_reasons",
                        field: "topology_reasons",
                        value: asText((topology?.reasons as string[] | undefined)?.join("; "))
                      }
                    ]}
                    pagination={false}
                    size="small"
                    columns={[
                      { title: "Field", dataIndex: "field", width: 160, key: "field" },
                      { title: "Value", dataIndex: "value", key: "value", render: (value: unknown) => <div className="table-wrap-cell">{asText(value)}</div> }
                    ]}
                  />
                )}
              </Card>
            )
          },
          {
            key: "plans",
            label: `Plan (${loadState === "loaded" ? plans.length : "—"})`,
            children: (
              <Card size="small">
                {plans.length === 0 ? (
                  <Alert message={loadState === "loaded" ? "已加载：未写入 plan.created 语义字段" : "Trace 未加载，Plan 状态未知"} showIcon type="info" />
                ) : (
                  <ResizableTable
                    dataSource={plans.map((row, index) => ({ ...row, key: `${String(row.plan_id || "p")}-${index}` }))}
                    pagination={{ pageSize: 8 }}
                    size="small"
                    columns={[
                      { title: "Plan", dataIndex: "plan_id", width: 140, key: "plan_id" },
                      { title: "Brief", dataIndex: "brief_id", width: 140, key: "brief_id" },
                      { title: "Tasks", dataIndex: "task_count", width: 80, key: "task_count" },
                      {
                        title: "Task IDs",
                        dataIndex: "task_ids",
                        key: "task_ids",
                        render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join(", ") : asText(value)}</div>
                      },
                      {
                        title: "Coverage missing",
                        dataIndex: "brief_coverage",
                        key: "brief_coverage",
                        render: (value: unknown) => {
                          const missing = (value as { missing_dimensions?: string[] } | undefined)?.missing_dimensions || [];
                          return <div className="table-wrap-cell">{missing.join(", ") || "—"}</div>;
                        }
                      }
                    ]}
                  />
                )}
              </Card>
            )
          },
          {
            key: "workers",
            label: `Worker (${loadState === "loaded" ? summary.worker_count || workers.length : "—"})`,
            children: (
              <Card size="small">
                {workers.length === 0 ? (
                  <Alert message={loadState === "loaded" ? "已加载：未写入 worker.started / worker.completed" : "Trace 未加载，Worker 状态未知"} showIcon type="info" />
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
                      { title: "s", dataIndex: "duration_ms", width: 100, key: "duration_ms", render: (value: unknown) => formatDurationSeconds(value) },
                      { title: "Attempt", dataIndex: "attempt", width: 90, key: "attempt" },
                      {
                        title: "Execution",
                        dataIndex: "execution_status",
                        width: 110,
                        key: "execution_status",
                        render: (value: unknown) => <Tag color={statusColor(value)}>{asText(value)}</Tag>
                      },
                      {
                        title: "Result",
                        dataIndex: "result_status",
                        width: 100,
                        key: "result_status",
                        render: (value: unknown) => <Tag color={value === "partial" ? "orange" : statusColor(value)}>{asText(value)}</Tag>
                      },
                      {
                        title: "Plan",
                        dataIndex: "plan_version",
                        width: 80,
                        key: "plan_version",
                        render: (version: unknown) => (version == null || version === "" ? "-" : `v${version}`)
                      },
                      {
                        title: "Evidence",
                        dataIndex: "evidence_ids",
                        width: 160,
                        key: "evidence_ids",
                        render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join(", ") : asText(value)}</div>
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
                      },
                      {
                        title: "Budget",
                        dataIndex: "budget_scope",
                        width: 180,
                        key: "budget",
                        render: (_value: unknown, row: Record<string, unknown>) => {
                          const scope = asText(row.budget_scope, "");
                          if (!scope) return <div className="table-wrap-cell">—</div>;
                          return (
                            <div className="table-wrap-cell">
                              {scope} / {asText(row.budget_resource, "")}
                              <br />
                              {asText(row.budget_reason, "")}
                            </div>
                          );
                        }
                      },
                      {
                        title: "Used / Limit",
                        dataIndex: "budget_used",
                        width: 120,
                        key: "budget_used",
                        render: (_value: unknown, row: Record<string, unknown>) => (
                          <div className="table-wrap-cell">
                            {row.budget_used == null ? "—" : `${asText(row.budget_used)} / ${asText(row.budget_limit, "?")}`}
                          </div>
                        )
                      },
                      {
                        title: "Last Tool Error",
                        dataIndex: "last_tool_error",
                        width: 180,
                        key: "last_tool_error",
                        render: (value: unknown) => {
                          if (!value) return <div className="table-wrap-cell">—</div>;
                          if (typeof value === "object") {
                            const row = value as { tool?: unknown; error?: unknown };
                            return <div className="table-wrap-cell">{`${asText(row.tool, "")}: ${asText(row.error, "")}`}</div>;
                          }
                          return <div className="table-wrap-cell">{asText(value, "")}</div>;
                        }
                      }
                    ]}
                  />
                )}
              </Card>
            )
          },
          {
            key: "supervisor",
            label: `Supervisor (${loadState === "loaded" ? supervisorDecisions.length : "—"}) / Coverage (${loadState === "loaded" ? coverageJudgements.length : "—"})`,
            children: (
              <Card size="small">
                {supervisorDecisions.length === 0 && coverageJudgements.length === 0 ? (
                  <Alert
                    message={loadState === "loaded" ? "已加载：尚未写入 supervisor.decided / coverage.assessed" : "Trace 未加载，Supervisor 状态未知"}
                    showIcon
                    type="info"
                  />
                ) : null}
                {summary.termination?.reason ? (
                  <Alert
                    message={`停止原因：${asText(summary.termination.reason)}`}
                    showIcon
                    style={{ marginBottom: 12 }}
                    type={String(summary.termination.outcome || "") === "failed" ? "error" : "info"}
                  />
                ) : null}
                <Typography.Title level={5}>Supervisor 决策</Typography.Title>
                {supervisorDecisions.length === 0 ? (
                  <Typography.Text type="secondary">没有 supervisor.decided 事件。</Typography.Text>
                ) : (
                  <ResizableTable
                    dataSource={supervisorDecisions.map((row, index) => ({ ...row, key: `sd-${index}` }))}
                    pagination={{ pageSize: 8 }}
                    size="small"
                    columns={[
                      {
                        title: "Action",
                        dataIndex: "action",
                        width: 150,
                        key: "action",
                        render: (value: unknown) => <Tag color={statusColor(value)}>{asText(value)}</Tag>
                      },
                      {
                        title: "Runtime",
                        dataIndex: "runtime_action",
                        width: 120,
                        key: "runtime_action",
                        render: (value: unknown) => <Tag color={statusColor(value)}>{asText(value)}</Tag>
                      },
                      {
                        title: "Reason",
                        dataIndex: "reason",
                        width: 280,
                        key: "reason",
                        render: (value: unknown) => <div className="table-wrap-cell">{asText(value)}</div>
                      },
                      { title: "Tasks", dataIndex: "task_count", width: 80, key: "task_count" },
                      { title: "Source", dataIndex: "source", width: 130, key: "source" },
                      {
                        title: "Runtime Reasons",
                        dataIndex: "runtime_reasons",
                        width: 260,
                        key: "runtime_reasons",
                        render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join(", ") : asText(value)}</div>
                      },
                      {
                        title: "Plan",
                        dataIndex: "plan_version",
                        width: 80,
                        key: "plan_version",
                        render: (version: unknown) => (version == null || version === "" ? "-" : `v${version}`)
                      },
                      {
                        title: "Time (local)",
                        dataIndex: "timestamp",
                        width: 180,
                        key: "timestamp",
                        render: (value: unknown) => <time dateTime={asText(value, "")}>{formatObservabilityTime(value)}</time>
                      }
                    ]}
                  />
                )}
                <Typography.Title level={5}>压缩发现</Typography.Title>
                {findings.length === 0 ? (
                  <Typography.Text type="secondary">没有 finding.compressed 事件。</Typography.Text>
                ) : (
                  <ResizableTable
                    dataSource={findings.map((row, index) => ({ ...row, key: `f-${index}` }))}
                    pagination={{ pageSize: 8 }}
                    size="small"
                    columns={[
                      { title: "Finding", dataIndex: "finding_id", width: 150, key: "finding_id" },
                      { title: "Task", dataIndex: "task_id", width: 160, key: "task_id" },
                      { title: "Claims", dataIndex: "claim_count", width: 90, key: "claim_count" },
                      { title: "Confidence", dataIndex: "confidence", width: 110, key: "confidence" },
                      {
                        title: "Evidence",
                        dataIndex: "evidence_ids",
                        key: "evidence_ids",
                        render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join(", ") : asText(value)}</div>
                      },
                      {
                        title: "Limitations",
                        dataIndex: "limitations",
                        key: "limitations",
                        render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join("; ") : asText(value)}</div>
                      }
                    ]}
                  />
                )}
                <Typography.Title level={5}>Coverage 判断</Typography.Title>
                {coverageJudgements.length === 0 ? (
                  <Typography.Text type="secondary">没有 coverage.assessed 事件。</Typography.Text>
                ) : (
                  <ResizableTable
                    dataSource={coverageJudgements.map((row, index) => ({ ...row, key: `cj-${index}` }))}
                    pagination={false}
                    size="small"
                    columns={[
                      {
                        title: "Status",
                        dataIndex: "status",
                        width: 110,
                        key: "status",
                        render: (value: unknown) => <Tag color={statusColor(value)}>{asText(value)}</Tag>
                      },
                      {
                        title: "Enough",
                        dataIndex: "sufficient",
                        width: 90,
                        key: "sufficient",
                        render: (value: unknown) => <Tag color={value === true ? "green" : "orange"}>{value === true ? "yes" : "no"}</Tag>
                      },
                      {
                        title: "Missing",
                        dataIndex: "missing",
                        width: 300,
                        key: "missing",
                        render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join("; ") : asText(value)}</div>
                      },
                      {
                        title: "Blocking gaps",
                        dataIndex: "blocking_gap_count",
                        width: 120,
                        key: "blocking_gap_count",
                        render: (value: unknown) => asText(value, "0")
                      },
                      {
                        title: "Key-question coverage",
                        dataIndex: "key_question_coverage",
                        width: 300,
                        key: "key_question_coverage",
                        render: (value: unknown) => {
                          if (!Array.isArray(value)) return "-";
                          return <div className="table-wrap-cell">{value.map((item) => {
                            const row = item as Record<string, unknown>;
                            return `${asText(row.question_id)}: ${asText(row.status)}${row.blocking === true ? " (blocking)" : ""}`;
                          }).join("; ")}</div>;
                        }
                      },
                      {
                        title: "Source quality",
                        dataIndex: "source_quality",
                        width: 220,
                        key: "source_quality",
                        render: (value: unknown) => {
                          const row = value as Record<string, unknown> | undefined;
                          if (!row || typeof row !== "object") return "-";
                          return <div className="table-wrap-cell">primary {asText(row.primary_source_ratio, "0")} · high-authority {asText(row.high_authority_source_ratio, "0")} · domains {asText(row.independent_source_count, "0")}</div>;
                        }
                      },
                      {
                        title: "Next Questions",
                        dataIndex: "recommended_next_questions",
                        width: 340,
                        key: "recommended_next_questions",
                        render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join("; ") : asText(value)}</div>
                      },
                      {
                        title: "Weak Claims",
                        dataIndex: "weak_claims",
                        width: 220,
                        key: "weak_claims",
                        render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join(", ") : asText(value)}</div>
                      },
                      {
                        title: "Conflicts",
                        dataIndex: "conflicts",
                        width: 220,
                        key: "conflicts",
                        render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join("; ") : asText(value, "0")}</div>
                      },
                      { title: "Source", dataIndex: "source", width: 120, key: "source" },
                      {
                        title: "Plan",
                        dataIndex: "plan_version",
                        width: 80,
                        key: "plan_version",
                        render: (version: unknown) => (version == null || version === "" ? "-" : `v${version}`)
                      }
                    ]}
                  />
                )}
                <Typography.Title level={5}>Progress 投影</Typography.Title>
                {progress.length > 0 ? (
                  <>
                    <ResizableTable
                      dataSource={progress.map((row, index) => ({ ...row, key: `p-${index}` }))}
                      pagination={false}
                      size="small"
                      columns={[
                        {
                          title: "Status",
                          dataIndex: "status",
                          width: 110,
                          key: "status",
                          render: (status: unknown) => <Tag color={statusColor(status)}>{asText(status)}</Tag>
                        },
                        {
                          title: "Plan",
                          dataIndex: "plan_version",
                          width: 80,
                          key: "plan_version",
                          render: (version: unknown) => (version == null || version === "" ? "-" : `v${version}`)
                        },
                        {
                          title: "Reasons",
                          dataIndex: "reason_codes",
                          width: 280,
                          key: "reason_codes",
                          render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join(", ") : asText(value)}</div>
                        },
                      {
                        title: "Actionable Gaps",
                        dataIndex: "semantic_gap_ids",
                        width: 280,
                        key: "semantic_gap_ids",
                        render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join(", ") : asText(value, "0")}</div>
                      },
                      {
                        title: "Unresolved",
                          dataIndex: "unresolved_gap_count",
                          width: 100,
                          key: "unresolved_gap_count",
                          render: (value: unknown) => asText(value, "0")
                        },
                        {
                          title: "Wave",
                          dataIndex: "dispatch_wave_id",
                          width: 80,
                          key: "dispatch_wave_id",
                          render: (value: unknown) => asText(value, "-")
                        },
                      {
                        title: "Conflicts",
                          dataIndex: "unresolved_conflicts",
                          width: 220,
                          key: "unresolved_conflicts",
                          render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join(", ") : asText(value, "0")}</div>
                        }
                      ]}
                    />
                  </>
                ) : null}
              </Card>
            )
          },
          {
            key: "synthesis",
            label: `Synthesis (${loadState === "loaded" ? synthesis.length : "—"})`,
            children: (
              <Card size="small">
                {synthesis.length === 0 ? (
                  <Alert message="尚未写入 synthesis.completed" showIcon type="info" />
                ) : (
                  <ResizableTable
                    dataSource={synthesis.map((row, index) => ({ ...row, key: `s-${index}` }))}
                    pagination={{ pageSize: 8 }}
                    size="small"
                    columns={[
                      { title: "Type", dataIndex: "type", width: 160, key: "type" },
                      { title: "Mode", dataIndex: "mode", width: 110, key: "mode" },
                      { title: "Attempt", dataIndex: "attempt", width: 90, key: "attempt" },
                      { title: "Duration (s)", dataIndex: "duration_ms", width: 120, key: "duration_ms", render: (value: unknown) => formatDurationSeconds(value) },
                      { title: "Input Tokens", dataIndex: "input_tokens_estimated", width: 130, key: "input_tokens_estimated" },
                      {
                        title: "Evidence",
                        dataIndex: "evidence_ids",
                        key: "evidence_ids",
                        render: (value: unknown) => <div className="table-wrap-cell">{Array.isArray(value) ? value.join(", ") : asText(value)}</div>
                      },
                      { title: "Fail Reason", dataIndex: "fail_reason", width: 180, key: "fail_reason", render: (value: unknown) => <div className="table-wrap-cell">{asText(value) || "-"}</div> },
                      { title: "Provider Class", dataIndex: "provider_failure_class", width: 170, key: "provider_failure_class", render: (value: unknown) => <div className="table-wrap-cell">{asText(value) || "-"}</div> },
                      { title: "Signals / Mechanisms", width: 150, key: "insights", render: (_: unknown, row: Record<string, unknown>) => `${asText(row.insight_signal_count, "0")} / ${asText(row.insight_mechanism_count, "0")}` },
                      { title: "Fallback", dataIndex: "fallback_action", width: 200, key: "fallback_action", render: (value: unknown) => <div className="table-wrap-cell">{asText(value) || "-"}</div> }
                    ]}
                  />
                )}
                {claimEvidenceBindings.length > 0 ? (
                  <>
                    <Typography.Title level={5} style={{ marginTop: 16 }}>Claim–Evidence Map</Typography.Title>
                    <Typography.Paragraph type="secondary">
                      正文只使用每项结论最强的 2–4 条证据；这里保留该结论的完整可追溯绑定。
                    </Typography.Paragraph>
                    <ResizableTable
                      dataSource={claimEvidenceBindings.map((row, index) => ({ ...row, key: `binding-${index}` }))}
                      pagination={{ pageSize: 8 }}
                      size="small"
                      columns={[
                        { title: "Claim", dataIndex: "claim_id", width: 120, key: "claim_id" },
                        { title: "Primary evidence", dataIndex: "primary_evidence_refs", key: "primary_evidence_refs", render: (value: unknown) => <div className="table-wrap-cell">{asText(value)}</div> },
                        { title: "Supporting evidence", dataIndex: "supporting_evidence_refs", key: "supporting_evidence_refs", render: (value: unknown) => <div className="table-wrap-cell">{asText(value)}</div> },
                        { title: "Counter / limitation", key: "constraints", render: (_: unknown, row: Record<string, unknown>) => <div className="table-wrap-cell">{asText([...(Array.isArray(row.counter_evidence_refs) ? row.counter_evidence_refs : []), ...(Array.isArray(row.limitation_refs) ? row.limitation_refs : [])])}</div> }
                      ]}
                    />
                  </>
                ) : null}
              </Card>
            )
          },
          {
            key: "citations",
            label: `证据源 / Evidence (${loadState === "loaded" ? citationsTotal : "—"})`,
            children: (
              <Card size="small">
                <Typography.Paragraph type="secondary">
                  Evidence 是本 Run 已登记的证据源与来源定位；它回答“用了哪些来源”，不表示最终结论的因果执行路径。
                </Typography.Paragraph>
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
                {citationsTotal > citations.length ? <Typography.Text type="secondary">已加载 {citations.length} / {citationsTotal}</Typography.Text> : null}
                {highlightSourceId ? (
                  <Alert
                    className="trace-citation-hint"
                    message={`已高亮 source_id=${highlightSourceId} 对应 execute 步骤；可在 Span Tree 中查看该步骤的 Related events`}
                    showIcon
                    type="success"
                  />
                ) : null}
              </Card>
            )
          },
          {
            key: "lineage",
            label: `Lineage (${loadState === "loaded" ? lineageTotal : "—"})`,
            children: (
              <Card size="small">
                <Typography.Paragraph type="secondary">
                  Lineage 是结论溯源：Claim / Finding / Evidence / Artifact / Source 之间的语义血缘，用于核查最终结论来自哪里。
                </Typography.Paragraph>
                {lineage.length === 0 ? (
                  <Alert message="尚无 semantic lineage edges（需要 brief/plan/worker/evidence/synthesis refs）" showIcon type="info" />
                ) : (
                  <><ResizableTable
                    dataSource={lineage}
                    rowKey={(row) => `${String(row.from_id || row.from || "")}-${String(row.to_id || row.to || "")}-${String(row.span_id || "")}`}
                    pagination={{ pageSize: 20 }}
                    size="small"
                    columns={[
                      {
                        title: "From",
                        key: "from",
                        render: (_: unknown, row: Record<string, unknown>) =>
                          `${asText(row.from_type)}:${asText(row.from_id)}`
                      },
                      {
                        title: "To",
                        key: "to",
                        render: (_: unknown, row: Record<string, unknown>) =>
                          `${asText(row.to_type)}:${asText(row.to_id)}`
                      },
                      { title: "Via", dataIndex: "via_event", key: "via_event", width: 180 },
                      { title: "Span", dataIndex: "span_id", key: "span_id", width: 140 }
                    ]}
                  />
                  {lineageTotal > lineage.length ? <Typography.Text type="secondary">已加载 {lineage.length} / {lineageTotal}</Typography.Text> : null}
                  </>
                )}
              </Card>
            )
          },
          {
            key: "tree",
            label: `Span Tree (${loadState === "loaded" ? traceTree.span_count || Number(summary.counts?.spans || 0) : "—"})`,
            children: (
              <div style={{ display: "grid", gridTemplateColumns: selectedSpan ? "1.2fr 0.8fr" : "1fr", gap: 12 }}>
                <Card size="small">
                  {typeof traceTree.omitted_count === "number" && traceTree.omitted_count > 0 ? (
                    <Alert
                      message={`已从因果树省略 ${traceTree.omitted_count} 条 llm_usage / gen_ai.chat，完整序列见 JSONL 页签。`}
                      showIcon
                      type="info"
                    />
                  ) : null}
                  <SpanTree
                    nodes={traceTree.roots || []}
                    onSelect={(node) => setSelectedSpan(node)}
                    selectedSpanId={selectedSpan?.span_id}
                  />
                </Card>
                {selectedSpan ? (
                  <Card
                    extra={
                      <Button onClick={() => setSelectedSpan(null)} size="small" type="link">
                        关闭
                      </Button>
                    }
                    size="small"
                    title="Span Detail"
                  >
                    <Typography.Paragraph>
                      <strong>{selectedSpan.name}</strong>
                      {selectedSpan.task_id ? ` · ${selectedSpan.task_id}` : ""}
                    </Typography.Paragraph>
                    <Typography.Paragraph type="secondary">
                      status={asText(selectedSpan.status)} · duration={formatDurationSeconds(selectedSpan.duration_ms)} · plan=
                      {asText(selectedSpan.plan_version)}
                    </Typography.Paragraph>
                    <Typography.Text strong>Related events</Typography.Text>
                    <ResizableTable
                      dataSource={jsonlEvents
                        .filter((event) => String(event.span_id || "") === String(selectedSpan.span_id || ""))
                        .slice(0, 40)
                        .map((event, index) => ({
                          key: `${event.event_id || index}`,
                          type: event.type || event.event,
                          status: event.status,
                          refs: [
                            ...((event.input_refs as Array<Record<string, unknown>> | undefined) || []).map(
                              (ref) => `in:${String(ref.type || "")}:${String(ref.id || "")}`
                            ),
                            ...((event.output_refs as Array<Record<string, unknown>> | undefined) || []).map(
                              (ref) => `out:${String(ref.type || "")}:${String(ref.id || "")}`
                            )
                          ].join(" | ")
                        }))}
                      pagination={false}
                      size="small"
                      columns={[
                        { title: "Type", dataIndex: "type", key: "type", width: 160 },
                        { title: "Status", dataIndex: "status", key: "status", width: 90 },
                        {
                          title: "Refs",
                          dataIndex: "refs",
                          key: "refs",
                          render: (value: unknown) => <div className="table-wrap-cell">{asText(value)}</div>
                        }
                      ]}
                    />
                  </Card>
                ) : null}
              </div>
            )
          },
          {
            key: "eval",
            label: `Run Quality Eval (${loadState === "loaded" ? evals.length : "—"})`,
            children: (
              <Card size="small">
                {summary.usage ? (
                  <Alert
                    message={`LLM ${summary.usage.calls || 0} calls · tokens ${summary.usage.total_tokens || 0} · cost $${Number(summary.usage.cost_usd || 0).toFixed(4)}`}
                    showIcon
                    type="info"
                  />
                ) : null}
                {summary.failure_counts && Object.keys(summary.failure_counts).length > 0 ? (
                  <Alert
                    message={`Failure attribution：${Object.entries(summary.failure_counts)
                      .map(([stage, count]) => `${stage} ${count}`)
                      .join(" · ")}`}
                    showIcon
                    type="warning"
                  />
                ) : null}
                {(summary.eval_matrix || []).map((matrix, index) => (
                  <div key={`eval-matrix-${index}`}>
                    <Typography.Title level={5}>Eval variants</Typography.Title>
                    <ResizableTable
                      dataSource={(matrix.cases || []).map((row) => ({ ...row, key: row.case_id }))}
                      pagination={false}
                      size="small"
                      columns={[
                        { title: "Case", dataIndex: "case_id", width: 120, key: "case_id" },
                        ...((matrix.variants || []).map((variant) => ({
                          title: variant,
                          key: variant,
                          width: 220,
                          render: (_: unknown, row: { variants?: Record<string, Record<string, unknown>> }) => {
                            const cell = row.variants?.[variant] || {};
                            return (
                              <div className="table-wrap-cell">
                                acc {String(cell.accuracy ?? "-")} · cite {String(cell.citation ?? "-")} · {formatDurationSeconds(cell.latency_ms)}
                              </div>
                            );
                          }
                        })))
                      ]}
                    />
                  </div>
                ))}
                {evals.length === 0 ? (
                  <Alert
                    message="交互提问不会产生 eval.scored（那是 tests/eval/run_eval.py --live）。Finalize 后应出现 quality.assessed；若仍为空，说明质量评估尚未发出或 run 未结束。"
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
                      { title: "s", dataIndex: "latency_ms", width: 100, key: "latency_ms", render: (value: unknown) => formatDurationSeconds(value) }
                    ]}
                  />
                )}
              </Card>
            )
          },
          {
            key: "jsonl",
            label: `JSONL (${loadState === "loaded" ? Number(summary.counts?.events || summary.event_count || 0) : "—"})`,
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
                    { title: "s", dataIndex: "duration_ms", width: 90, key: "duration_ms", render: (value: unknown) => formatDurationSeconds(value) },
                    {
                      title: "Time (local)",
                      dataIndex: "timestamp",
                      width: 180,
                      key: "timestamp",
                      render: (value: unknown) => <time dateTime={asText(value, "")}>{formatObservabilityTime(value)}</time>
                    }
                  ]}
                />
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

export const TraceViewer = memo(TraceViewerImpl);
