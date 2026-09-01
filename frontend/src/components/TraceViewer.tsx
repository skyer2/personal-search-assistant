import { LinkOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Space, Table, Tabs, Tag, Typography } from "antd";
import { useCallback, useEffect, useState } from "react";
import {
  fetchCitations,
  fetchJsonlTrace,
  fetchLangfuseConfig,
  fetchLangfuseTraces
} from "../lib/api";
import type { EvidenceSource, JsonlTraceEvent, TraceSpanNode, TraceTree } from "../types";

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
      const [jsonl, citationsResp, lfConfig, lfTraces] = await Promise.all([
        fetchJsonlTrace(sessionId),
        fetchCitations(sessionId),
        fetchLangfuseConfig(),
        fetchLangfuseTraces(sessionId).catch(() => ({
          enabled: false,
          traces: [],
          message: "Langfuse 请求失败"
        }))
      ]);
      setJsonlEvents(jsonl.events || []);
      setJsonlMessage(jsonl.message || "");
      setTraceTree(
        jsonl.tree ||
          (lfTraces as { tree?: TraceTree }).tree || { roots: [], span_count: 0, event_count: 0 }
      );
      setCitations(citationsResp.sources || []);
      setCitationsMessage(citationsResp.message || "");
      setLangfuseEnabled(Boolean(lfConfig.enabled));
      setLangfuseUrl(lfConfig.ui_url || lfConfig.host || null);
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

  return (
    <div className="trace-viewer">
      <div className="panel-heading-row">
        <div>
          <span className="panel-kicker">OBSERVABILITY</span>
          <Typography.Title level={4}>Trace 查看器</Typography.Title>
          <Typography.Text type="secondary">session_id = {sessionId}</Typography.Text>
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
                <SpanTree nodes={traceTree.roots || []} />
              </Card>
            )
          },
          {
            key: "jsonl",
            label: `JSONL (${jsonlEvents.length})`,
            children: (
              <Card size="small">
                {jsonlMessage ? <Alert message={jsonlMessage} showIcon type="info" /> : null}
                <Table
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
                      render: (_, row) => String(row.type || row.event || row.phase || "-"),
                      width: 160
                    },
                    { title: "Phase", dataIndex: "phase", width: 110 },
                    { title: "Status", dataIndex: "status", width: 90 },
                    { title: "Task", dataIndex: "task_id", width: 90 },
                    {
                      title: "Step",
                      render: (_, row) => (typeof row.step_index === "number" ? row.step_index + 1 : "-")
                    },
                    { title: "ms", dataIndex: "duration_ms", width: 80 },
                    { title: "Time", dataIndex: "timestamp", ellipsis: true }
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
                <Table
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
                      width: 64,
                      render: (num: number) => <Tag color="blue">[{num}]</Tag>
                    },
                    {
                      title: "类型",
                      dataIndex: "source_kind",
                      width: 80,
                      render: (kind: string) => <Tag>{kind}</Tag>
                    },
                    {
                      title: "Step",
                      render: (_, row) => `${row.step_index + 1} / ${row.step_type}`
                    },
                    {
                      title: "来源",
                      dataIndex: "locator",
                      ellipsis: true,
                      render: (locator: string, row) =>
                        row.source_kind === "url" ? (
                          <a href={locator} rel="noreferrer" target="_blank">
                            {locator}
                          </a>
                        ) : (
                          locator
                        )
                    },
                    {
                      title: "操作",
                      width: 100,
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
