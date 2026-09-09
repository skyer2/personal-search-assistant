import { CheckOutlined, CloseOutlined, EditOutlined, PauseCircleOutlined } from "@ant-design/icons";
import { Button, Card, Input, Space, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import type { HitlInterruptPayload } from "../types";

interface ApprovalPanelProps {
  payload: HitlInterruptPayload;
  isSubmitting: boolean;
  onApproveAll: () => void;
  onRejectAll: () => void;
  onDecide: (
    decisions: Array<{
      type: "approve" | "reject" | "edit";
      edited_action?: Record<string, unknown>;
    }>
  ) => void;
}

type EditableStep = {
  step_type: string;
  description: string;
  subagent?: string | null;
};

export function ApprovalPanel({
  payload,
  isSubmitting,
  onApproveAll,
  onRejectAll,
  onDecide
}: ApprovalPanelProps) {
  const gateLabel =
    payload.gate_type === "plan_review"
      ? "计划审批"
      : payload.gate_type === "intent_clarification"
        ? "意图澄清"
        : payload.gate_type === "step"
          ? "步骤审批"
          : "工具 interrupt_on 审批";

  const isPlanReview = payload.gate_type === "plan_review";
  const isIntentClarification = payload.gate_type === "intent_clarification";
  const isEditable = Boolean(payload.editable) || isPlanReview || isIntentClarification;

  const [editedDescription, setEditedDescription] = useState("");
  const [planStepsJson, setPlanStepsJson] = useState("");
  const [intentJson, setIntentJson] = useState("");

  useEffect(() => {
    const first = payload.action_requests[0];
    if (!first) {
      return;
    }
    const desc = String(first.args?.description || "");
    setEditedDescription(desc);
    if (isPlanReview && Array.isArray(first.args?.steps)) {
      setPlanStepsJson(JSON.stringify(first.args.steps, null, 2));
    }
    if (isIntentClarification && first.args?.intent) {
      setIntentJson(JSON.stringify(first.args.intent, null, 2));
    }
  }, [payload, isPlanReview, isIntentClarification]);

  function buildEditDecision(): { type: "edit"; edited_action: Record<string, unknown> } {
    const edited: Record<string, unknown> = {};
    if (isPlanReview) {
      try {
        const steps = JSON.parse(planStepsJson) as EditableStep[];
        edited.steps = steps;
      } catch {
        edited.steps = payload.action_requests[0]?.args?.steps;
      }
    } else if (isIntentClarification) {
      try {
        edited.intent = JSON.parse(intentJson) as Record<string, unknown>;
      } catch {
        edited.intent = payload.action_requests[0]?.args?.intent;
      }
    } else if (editedDescription.trim()) {
      edited.description = editedDescription.trim();
    }
    return { type: "edit", edited_action: edited };
  }

  return (
    <Card
      className="hitl-approval-panel"
      title={
        <Space>
          <PauseCircleOutlined aria-hidden />
          <span>任务已暂停 · 需要人工审批</span>
          <Tag color="gold">{gateLabel}</Tag>
          {isEditable ? <Tag>可编辑后继续</Tag> : null}
        </Space>
      }
    >
      <Typography.Paragraph type="secondary">
        {isIntentClarification
          ? "任务理解存在歧义或置信度较低。确认前执行已冻结，进度条不会继续跳动。"
          : "高风险步骤已在副作用前冻结。批准、拒绝或编辑后才会继续，这与 GitHub / LangGraph interrupt 的审批流一致。"}
      </Typography.Paragraph>

      <ul className="hitl-action-list">
        {payload.action_requests.map((action, index) => (
          <li className="hitl-action-item" key={`${action.name}-${index}`}>
            <div className="hitl-action-head">
              <strong>{action.name}</strong>
              {typeof payload.step_index === "number" && payload.step_index >= 0 ? (
                <Tag>Step {payload.step_index + 1}</Tag>
              ) : null}
            </div>

            {isIntentClarification && isEditable ? (
              <div className="hitl-edit-block">
                <Typography.Text type="secondary">
                  {String(action.args?.question || "请确认任务意图")}
                </Typography.Text>
                <Typography.Text type="secondary">编辑 intent JSON（deliverable: text|md|pdf）</Typography.Text>
                <Input.TextArea
                  autoSize={{ minRows: 8, maxRows: 16 }}
                  disabled={isSubmitting}
                  onChange={(event) => setIntentJson(event.target.value)}
                  value={intentJson}
                />
              </div>
            ) : isPlanReview && isEditable ? (
              <div className="hitl-edit-block">
                <Typography.Text type="secondary">编辑执行计划（JSON 数组）</Typography.Text>
                <Input.TextArea
                  autoSize={{ minRows: 6, maxRows: 14 }}
                  disabled={isSubmitting}
                  onChange={(event) => setPlanStepsJson(event.target.value)}
                  value={planStepsJson}
                />
              </div>
            ) : (
              <>
                <pre className="hitl-action-args">{JSON.stringify(action.args, null, 2)}</pre>
                {isEditable && payload.gate_type === "step" ? (
                  <div className="hitl-edit-block">
                    <Typography.Text type="secondary">编辑步骤描述</Typography.Text>
                    <Input.TextArea
                      autoSize={{ minRows: 2, maxRows: 6 }}
                      disabled={isSubmitting}
                      onChange={(event) => setEditedDescription(event.target.value)}
                      value={editedDescription}
                    />
                  </div>
                ) : null}
              </>
            )}

            <Space wrap>
              <Button
                disabled={isSubmitting}
                icon={<CheckOutlined aria-hidden />}
                onClick={() => onDecide([{ type: "approve" }])}
                size="small"
                type="primary"
              >
                批准
              </Button>
              {isEditable ? (
                <Button
                  disabled={isSubmitting}
                  icon={<EditOutlined aria-hidden />}
                  onClick={() => onDecide([buildEditDecision()])}
                  size="small"
                >
                  应用编辑
                </Button>
              ) : null}
              <Button
                danger
                disabled={isSubmitting}
                icon={<CloseOutlined aria-hidden />}
                onClick={() => onDecide([{ type: "reject" }])}
                size="small"
              >
                拒绝
              </Button>
            </Space>
          </li>
        ))}
      </ul>

      {payload.action_requests.length > 1 ? (
        <Space className="hitl-bulk-actions">
          <Button loading={isSubmitting} onClick={onApproveAll} type="primary">
            全部批准
          </Button>
          <Button danger loading={isSubmitting} onClick={onRejectAll}>
            全部拒绝
          </Button>
        </Space>
      ) : null}
    </Card>
  );
}
