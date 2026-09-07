import { DatabaseOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Popconfirm, Space, Table, Tag, Typography } from "antd";
import { useCallback, useEffect, useState } from "react";
import {
  fetchMemoryRecords,
  forgetMemoryRecord,
  forgetSessionMemory,
  forgetUserMemory
} from "../lib/api";
import type { MemoryRecord } from "../types";

interface MemoryPanelProps {
  sessionId?: string;
}

export function MemoryPanel({ sessionId }: MemoryPanelProps) {
  const { message } = AntApp.useApp();
  const [records, setRecords] = useState<MemoryRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetchMemoryRecords({
        tenantId: "local",
        userId: "me",
        projectId: "Inbox"
      });
      setRecords(response.records || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载长期记忆失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleAction(action: () => Promise<unknown>, successMessage: string) {
    setActing(true);
    setError("");
    try {
      await action();
      await load();
      message.success(successMessage);
    } catch (err) {
      setError(err instanceof Error ? err.message : "记忆操作失败");
    } finally {
      setActing(false);
    }
  }

  return (
    <div className="memory-panel">
      <div className="panel-heading-row">
        <div>
          <span className="panel-kicker">LONG-TERM MEMORY</span>
          <Typography.Title level={4}>Agent Memory</Typography.Title>
        </div>
        <Space>
          <Button icon={<ReloadOutlined aria-hidden />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
          <Button
            disabled={!sessionId}
            loading={acting}
            onClick={() =>
              sessionId
                ? void handleAction(
                    () => forgetSessionMemory(sessionId),
                    "已遗忘当前会话派生的长期记忆"
                  )
                : undefined
            }
          >
            遗忘当前会话
          </Button>
          <Popconfirm
            title="遗忘全部长期记忆"
            description="聊天历史会保留，但 Agent 将不再召回这些记忆。"
            okText="确认遗忘"
            okButtonProps={{ danger: true }}
            onConfirm={() => void handleAction(forgetUserMemory, "已清空当前用户长期记忆")}
          >
            <Button danger loading={acting}>
              清空用户记忆
            </Button>
          </Popconfirm>
        </Space>
      </div>

      {error ? <Alert message={error} showIcon type="error" /> : null}
      {records.length === 0 && !loading ? (
        <Alert message="当前用户没有可召回的长期记忆。" showIcon type="info" />
      ) : null}

      <Card size="small" title={`记忆记录（${records.length}）`} loading={loading}>
        <Table
          dataSource={records.map((record) => ({ ...record, key: record.id }))}
          pagination={{ pageSize: 10, showSizeChanger: false }}
          size="small"
          columns={[
            {
              title: "记忆",
              dataIndex: "fact",
              render: (fact: string) => <Typography.Text>{fact}</Typography.Text>
            },
            {
              title: "类型",
              dataIndex: "memory_type",
              width: 110,
              render: (value: string) => <Tag>{value}</Tag>
            },
            {
              title: "信任级",
              dataIndex: "trust_tier",
              width: 110,
              render: (value: string) => <Tag color={value === "trusted" ? "success" : value === "untrusted" ? "error" : "processing"}>{value}</Tag>
            },
            { title: "Project", dataIndex: "project_id", width: 110 },
            { title: "Session", dataIndex: "session_id", width: 150, ellipsis: true },
            {
              title: "Run",
              width: 150,
              render: (_value: unknown, record: MemoryRecord) => record.provenance?.run_id || "-"
            },
            {
              title: "操作",
              width: 100,
              render: (_value: unknown, record: MemoryRecord) => (
                <Popconfirm
                  title="遗忘这条记忆"
                  okText="确认"
                  okButtonProps={{ danger: true }}
                  onConfirm={() => void handleAction(() => forgetMemoryRecord(record.id), "已遗忘该记忆")}
                >
                  <Button danger loading={acting} size="small">
                    遗忘
                  </Button>
                </Popconfirm>
              )
            }
          ]}
        />
      </Card>

      <Typography.Paragraph type="secondary">
        <DatabaseOutlined aria-hidden /> 会话历史、上下文选择和长期记忆是三层独立数据；删除问答会级联删除对应 Run 派生记忆，但不会自动删除用户显式偏好的全部记忆。
      </Typography.Paragraph>
    </div>
  );
}
