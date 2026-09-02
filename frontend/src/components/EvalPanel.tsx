import { BarChartOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Space, Statistic, Table, Tag, Typography } from "antd";
import { useCallback, useEffect, useState } from "react";
import { fetchEvalBaseline, fetchEvalLatest, runEvalDryRun } from "../lib/api";
import type { EvalReport } from "../types";

const METRIC_ROWS = [
  { key: "task_success_rate", label: "Gate", suffix: "%", scale: 100 },
  { key: "outcome_score", label: "Outcome", suffix: "%", scale: 100 },
  { key: "grounding_score", label: "Grounding", suffix: "%", scale: 100 },
  { key: "trajectory_score", label: "Trajectory", suffix: "%", scale: 100 },
  { key: "plan_validation_pass_rate", label: "Invariants", suffix: "%", scale: 100 },
  { key: "pass_at_1", label: "pass@1", suffix: "%", scale: 100 },
  { key: "pass_hat_k", label: "pass^k", suffix: "%", scale: 100 },
  { key: "citation_coverage_rate", label: "CCR", suffix: "%", scale: 100 },
  { key: "avg_tool_calls", label: "Tool Calls", suffix: "", scale: 1 },
  { key: "latency_p95_ms", label: "P95", suffix: "ms", scale: 1 }
] as const;

function formatMetric(report: EvalReport, key: string, scale: number, suffix: string): string {
  const value = report[key as keyof EvalReport];
  if (typeof value !== "number") {
    return "-";
  }
  const scaled = suffix === "%" ? value * scale : value;
  return suffix === "%" ? `${scaled.toFixed(1)}%` : `${scaled.toFixed(suffix === "ms" ? 0 : 2)}${suffix}`;
}

export function EvalPanel() {
  const [report, setReport] = useState<EvalReport | null>(null);
  const [baseline, setBaseline] = useState<EvalReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [latest, base] = await Promise.all([
        fetchEvalLatest().catch(() => null),
        fetchEvalBaseline().catch(() => null)
      ]);
      setReport(latest);
      setBaseline(base);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载 Eval 数据失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleRunDryEval() {
    setRunning(true);
    setError("");
    try {
      await runEvalDryRun();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "触发 Eval 失败");
    } finally {
      setRunning(false);
    }
  }

  const comparison = report?.baseline_comparison?.deltas;

  return (
    <div className="eval-panel">
      <div className="panel-heading-row">
        <div>
          <span className="panel-kicker">HARNESS EVAL</span>
          <Typography.Title level={4}>Harness Eval</Typography.Title>
        </div>
        <Space>
          <Button icon={<ReloadOutlined aria-hidden />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
          <Button
            icon={<BarChartOutlined aria-hidden />}
            loading={running}
            onClick={() => void handleRunDryEval()}
            type="primary"
          >
            运行 Dry-run Eval
          </Button>
        </Space>
      </div>

      {error ? <Alert message={error} showIcon type="error" /> : null}

      <div className="eval-stats-grid">
        {METRIC_ROWS.map((metric) => (
          <Card key={metric.key} loading={loading} size="small">
            <Statistic
              title={metric.label}
              value={report ? formatMetric(report, metric.key, metric.scale, metric.suffix) : "-"}
            />
            {comparison && typeof comparison[metric.key] === "number" ? (
              <Tag color={(comparison[metric.key] ?? 0) >= 0 ? "success" : "error"}>
                Δ {(comparison[metric.key] ?? 0) >= 0 ? "+" : ""}
                {metric.suffix === "%"
                  ? `${(((comparison[metric.key] ?? 0) as number) * 100).toFixed(1)}%`
                  : ((comparison[metric.key] ?? 0) as number).toFixed(3)}
              </Tag>
            ) : null}
          </Card>
        ))}
      </div>

      <Card size="small" title="任务明细" loading={loading}>
        <Table
          dataSource={(report?.results || []).map((item) => ({ ...item, key: item.task_id }))}
          pagination={false}
          size="small"
          columns={[
            { title: "ID", dataIndex: "task_id", width: 160 },
            {
              title: "结果",
              dataIndex: "success",
              render: (success: boolean) => (
                <Tag color={success ? "success" : "error"}>{success ? "PASS" : "FAIL"}</Tag>
              )
            },
            { title: "Status", dataIndex: "status" },
            { title: "Retry", dataIndex: "retry_count", width: 72 },
            { title: "Stage", dataIndex: "failure_stage", width: 100 },
            { title: "Type", dataIndex: "failure_type" }
          ]}
        />
      </Card>

      {baseline ? (
        <Typography.Paragraph type="secondary">
          基线：{baseline.generated_at || "unknown"} / mode={baseline.mode || "dry-run"}
        </Typography.Paragraph>
      ) : null}
    </div>
  );
}
