import { PauseCircleOutlined } from "@ant-design/icons";
import type { ReactNode } from "react";
import { PHASE_LABELS, type PhaseProgress } from "../lib/phaseProgress";
import { describeRunOutcome, type RunStatus } from "../lib/runStatus";

interface RunProgressProps {
  durationLabel: ReactNode;
  quality?: Record<string, unknown>;
  termination?: Record<string, unknown>;
  progress: PhaseProgress;
  runStatus: RunStatus;
}

export function RunProgress({
  durationLabel,
  progress,
  quality,
  termination,
  runStatus,
}: RunProgressProps) {
  const paused = runStatus === "awaiting_approval";
  const cancelling = runStatus === "cancelling";
  const live = runStatus === "running";
  const executePhase = progress.items.find((item) => item.phase === "research");
  const researchSkipped =
    !progress.stageOrder.includes("fast_research") &&
    (!executePhase || executePhase.tone === "idle" || ["skipped", "not_started"].includes(executePhase.status));
  const qualityFailed =
    progress.items.some((item) => item.phase === "quality" && item.tone === "failed") ||
    progress.hasFailed;
  const qualityRepairable = quality?.repairable === true;
  const outcome = describeRunOutcome({ runStatus, quality, termination });

  const title = paused
    ? "已暂停 · 等待人工审批"
    : cancelling
      ? "正在取消当前任务"
      : runStatus === "completed"
        ? "运行完成"
        : runStatus === "partial"
          ? outcome?.title ?? "部分可确认"
        : runStatus === "failed"
          ? "执行失败"
          : runStatus === "interrupted"
            ? "执行已中断"
          : runStatus === "unknown"
            ? "运行已结束 · 状态未知"
            : "正在运行";

  const detail = paused
    ? "计时与进度已冻结，审批通过后才会继续"
      : runStatus === "completed"
      ? "终端状态为完成；语义阶段按真实事件投影。"
      : runStatus === "partial"
        ? [
            outcome?.detail ?? "已保留可确认结论，但未达到完整交付标准。",
            qualityFailed && qualityRepairable ? "可修复" : "",
            researchSkipped ? "Research Skipped / 未执行" : "",
            progress.stepHint || "",
          ]
            .filter(Boolean)
            .join(" · ")
      : progress.stepHint
        ? `${progress.currentLabel} · ${progress.stepHint}`
        : runStatus === "unknown"
          ? "终端事件缺少状态字段，已保留真实阶段进度。"
          : progress.currentLabel;

  return (
    <div
      className={`run-progress ${paused ? "run-progress--paused" : ""} ${live ? "run-progress--live" : ""}`}
      aria-live="polite"
      aria-label={title}
    >
      <div className="run-progress-status">
        {paused ? (
          <PauseCircleOutlined className="run-progress-icon" aria-hidden />
        ) : (
          <span className="run-progress-dot" aria-hidden />
        )}
        <strong>{title}</strong>
        <span className="run-progress-duration">
          {paused ? <>暂停于 {durationLabel}</> : <>已用时 {durationLabel}</>}
        </span>
        <span className="run-progress-percent">{progress.percent}%</span>
      </div>

      <div
        className={`progress-bar ${paused ? "progress-bar--paused" : "progress-bar--determinate"}`}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress.percent}
        aria-valuetext={`${progress.currentLabel} ${progress.percent}%`}
      >
        <span className="progress-bar-fill" style={{ width: `${Math.max(progress.percent, 4)}%` }} />
      </div>

      <ol className="phase-pipeline" aria-label="研究语义阶段">
        {progress.stageOrder.map((phase) => {
          const item = progress.items.find((entry) => entry.phase === phase);
          const tone = item?.tone ?? "idle";
          return (
            <li className={`phase-pipeline-item phase-pipeline-item--${tone}`} key={phase}>
              <span className="phase-pipeline-dot" />
              <span>
                {PHASE_LABELS[phase]}
                {item?.status ? <small> · {item.status}</small> : null}
              </span>
            </li>
          );
        })}
      </ol>

      <p className="run-progress-detail">{detail}</p>
    </div>
  );
}
