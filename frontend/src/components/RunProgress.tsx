import { PauseCircleOutlined } from "@ant-design/icons";
import { PHASE_LABELS, PHASE_ORDER, type PhaseProgress } from "../lib/phaseProgress";
import { type RunStatus } from "../lib/runStatus";

interface RunProgressProps {
  durationLabel: string;
  progress: PhaseProgress;
  runStatus: RunStatus;
}

export function RunProgress({ durationLabel, progress, runStatus }: RunProgressProps) {
  const paused = runStatus === "awaiting_approval";
  const cancelling = runStatus === "cancelling";
  const live = runStatus === "running";

  const title = paused
    ? "已暂停 · 等待人工审批"
    : cancelling
      ? "正在取消当前任务"
      : runStatus === "completed"
        ? "运行完成"
        : runStatus === "failed"
          ? "任务失败"
          : "正在运行";

  const detail = paused
    ? "计时与进度已冻结，审批通过后才会继续"
    : runStatus === "completed"
      ? "全部阶段已完成"
      : progress.stepHint
        ? `${progress.currentLabel} · ${progress.stepHint}`
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
          {paused ? `暂停于 ${durationLabel}` : `已用时 ${durationLabel}`}
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

      <ol className="phase-pipeline" aria-label="Harness 阶段">
        {PHASE_ORDER.map((phase) => {
          const item = progress.items.find((entry) => entry.phase === phase);
          const tone = item?.tone ?? "idle";
          return (
            <li className={`phase-pipeline-item phase-pipeline-item--${tone}`} key={phase}>
              <span className="phase-pipeline-dot" />
              <span>{PHASE_LABELS[phase]}</span>
            </li>
          );
        })}
      </ol>

      <p className="run-progress-detail">{detail}</p>
    </div>
  );
}
