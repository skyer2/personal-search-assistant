import { BarChartOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Space, Statistic, Table, Tag, Typography } from "antd";
import { useCallback, useEffect, useState } from "react";
import { fetchEvalBaseline, fetchEvalLatest, runEvalDryRun } from "../lib/api";
import type { EvalReport, RegressionSummary } from "../types";

const COMPONENT_LABELS: Array<{ key: string; label: string }> = [
  { key: "brief", label: "Brief" },
  { key: "coverage", label: "Coverage" },
  { key: "supervisor", label: "Supervisor" },
  { key: "evidence", label: "Evidence" }
];

const LIVE_ONLY_METRICS = [
  "Grounding",
  "Citation P/R",
  "CCR",
  "Unsupported Claim Rate",
  "Tool Calls",
  "Tokens",
  "Cost",
  "P50/P95",
  "pass@1",
  "pass^k"
];

function summaryValue(summary?: RegressionSummary): string {
  if (!summary || !summary.total) {
    return "N/A";
  }
  return `${summary.passed ?? 0} / ${summary.total}`;
}

function isPassed(summary?: RegressionSummary): boolean {
  return Boolean(summary?.total && summary.passed === summary.total);
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
      setError(err instanceof Error ? err.message : "加载回归数据失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleRunRegressionGate() {
    setRunning(true);
    setError("");
    try {
      await runEvalDryRun();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "触发回归评测失败");
    } finally {
      setRunning(false);
    }
  }

  const regression = report?.regression_summary;
  const comparison = report?.baseline_comparison;
  const regressions = comparison?.regressions ?? [];

  return (
    <div className="eval-panel">
      <div className="panel-heading-row">
        <div>
          <span className="panel-kicker">DETERMINISTIC REGRESSION</span>
          <Typography.Title level={4}>Regression Gate</Typography.Title>
        </div>
        <Space>
          <Button icon={<ReloadOutlined aria-hidden />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
          <Button
            icon={<BarChartOutlined aria-hidden />}
            loading={running}
            onClick={() => void handleRunRegressionGate()}
            type="primary"
          >
            运行回归 Gate
          </Button>
        </Space>
      </div>

      {error ? <Alert message={error} showIcon type="error" /> : null}
      {report?.mode && report.mode !== "dry-run" ? (
        <Alert
          message="当前最新报告不是 dry-run。这个面板只解释确定性回归；真实能力指标请在 Live E2E 报告中查看。"
          showIcon
          type="warning"
        />
      ) : null}

      <div className="eval-stats-grid">
        <Card loading={loading} size="small">
          <Statistic title="Component" value={summaryValue(regression?.component)} />
          <Tag color={isPassed(regression?.component) ? "success" : "error"}>
            {isPassed(regression?.component) ? "PASS" : "FAIL"}
          </Tag>
        </Card>
        <Card loading={loading} size="small">
          <Statistic title="Scenario" value={summaryValue(regression?.scenario)} />
          <Tag color={isPassed(regression?.scenario) ? "success" : "error"}>
            {isPassed(regression?.scenario) ? "PASS" : "FAIL"}
          </Tag>
        </Card>
        {COMPONENT_LABELS.map((component) => {
          const summary = regression?.by_component?.[component.key];
          return (
            <Card key={component.key} loading={loading} size="small">
              <Statistic title={component.label} value={summaryValue(summary)} />
              <Tag color={isPassed(summary) ? "success" : "error"}>
                {isPassed(summary) ? "PASS" : "FAIL"}
              </Tag>
            </Card>
          );
        })}
        <Card loading={loading} size="small">
          <Statistic title="Invariants" value={report ? `${Math.round((report.plan_validation_pass_rate ?? 0) * 100)}%` : "N/A"} />
        </Card>
        <Card loading={loading} size="small">
          <Statistic title="Baseline Regression" value={regressions.length} />
        </Card>
        <Card loading={loading} size="small">
          <Statistic title="Merge Blocked" value={comparison?.blocked_merge ? "YES" : "NO"} />
        </Card>
      </div>

      <Card size="small" title="Live E2E 专属指标（Dry-run 不测量）">
        <Space size={[8, 8]} wrap>
          {LIVE_ONLY_METRICS.map((metric) => (
            <Tag key={metric}>{metric}: N/A</Tag>
          ))}
        </Space>
        <Typography.Paragraph type="secondary">
          Dry-run 只验证 Planner / Progress / Replan / Evidence / Scenario 的确定性约束，不调用真实搜索与 LLM，因此不产生真实答案质量、成本或延迟结论。
        </Typography.Paragraph>
      </Card>

      <Card size="small" title="回归明细" loading={loading}>
        <Table
          dataSource={(report?.results || []).map((item) => ({ ...item, key: item.task_id }))}
          pagination={false}
          size="small"
          columns={[
            { title: "Layer", dataIndex: "mode", width: 110 },
            { title: "ID", dataIndex: "task_id", width: 170 },
            {
              title: "结果",
              dataIndex: "success",
              width: 90,
              render: (success: boolean) => (
                <Tag color={success ? "success" : "error"}>{success ? "PASS" : "FAIL"}</Tag>
              )
            },
            { title: "Status", dataIndex: "status" },
            { title: "Stage", dataIndex: "failure_stage", width: 110 },
            { title: "Type", dataIndex: "failure_type" }
          ]}
        />
      </Card>

      {baseline ? (
        <Typography.Paragraph type="secondary">
          基线：{baseline.generated_at || "unknown"} / mode={baseline.mode || "dry-run"}。该基线证明控制逻辑不退化，不证明线上答案质量。
        </Typography.Paragraph>
      ) : null}
    </div>
  );
}
