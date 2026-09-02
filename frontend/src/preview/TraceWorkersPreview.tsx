import { Card, Tag, Typography } from "antd";
import { ResizableTable } from "../components/ResizableTable";

const workers = [
  {
    key: "w-1",
    task_id: "T1",
    status: "ok",
    duration_ms: 18420,
    attempt: 1,
    plan_version: 2,
    objective:
      "汇总 METR 时间视野倍增、FutureSearch 对 AI R&D uplift 的中位预测，以及 Anthropic 刹车踏板公开表态。需要完整可读，不能被单元格截断。",
    fail_reason: ""
  },
  {
    key: "w-2",
    task_id: "T2",
    status: "failed",
    duration_ms: 22100,
    attempt: 2,
    plan_version: 2,
    objective: "核对 Guidelight 五大实验室控制实践评分，并列出 containment plan 得 0 分的条目。",
    fail_reason: "deadline_exceeded：预算用尽，未完成全部来源交叉验证"
  }
];

export function TraceWorkersPreview() {
  return (
    <div className="trace-viewer" style={{ padding: 24 }}>
      <span className="panel-kicker">DEV PREVIEW</span>
      <Typography.Title level={4}>Worker 表列宽</Typography.Title>
      <Typography.Text type="secondary">拖动表头右侧细条可调整列宽，Objective / Fail 应自动换行。</Typography.Text>
      <Card size="small" style={{ marginTop: 16 }}>
        <ResizableTable
          dataSource={workers}
          pagination={false}
          size="small"
          columns={[
            { title: "Task", dataIndex: "task_id", width: 180, key: "task_id" },
            {
              title: "Status",
              dataIndex: "status",
              width: 90,
              key: "status",
              render: (status: unknown) => <Tag color={status === "ok" ? "green" : "red"}>{String(status)}</Tag>
            },
            { title: "ms", dataIndex: "duration_ms", width: 100, key: "duration_ms" },
            { title: "Attempt", dataIndex: "attempt", width: 90, key: "attempt" },
            {
              title: "Plan",
              dataIndex: "plan_version",
              width: 80,
              key: "plan_version",
              render: (version: unknown) => `v${version}`
            },
            {
              title: "Objective",
              dataIndex: "objective",
              width: 420,
              key: "objective",
              render: (value: unknown) => <div className="table-wrap-cell">{String(value)}</div>
            },
            {
              title: "Fail",
              dataIndex: "fail_reason",
              width: 180,
              key: "fail_reason",
              render: (value: unknown) => <div className="table-wrap-cell">{String(value || "")}</div>
            }
          ]}
        />
      </Card>
    </div>
  );
}
