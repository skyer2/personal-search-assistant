import { DownloadOutlined, FileMarkdownOutlined, FilePdfOutlined, FileTextOutlined } from "@ant-design/icons";
import { Button, Tooltip } from "antd";
import { getDownloadUrl } from "../lib/api";
import type { OutputFile } from "../types";

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function FileIcon({ name }: { name: string }) {
  if (name.endsWith(".pdf")) {
    return <FilePdfOutlined aria-hidden />;
  }
  if (name.endsWith(".md")) {
    return <FileMarkdownOutlined aria-hidden />;
  }
  return <FileTextOutlined aria-hidden />;
}

function isPrimaryDeliverable(file: OutputFile): boolean {
  const name = file.name.toLowerCase();
  if (name === "working_notes.md" || name === "working_notes.pdf") {
    return false;
  }
  return name.endsWith(".pdf") || name.endsWith(".md");
}

export function sortDeliverableFiles(files: OutputFile[]): OutputFile[] {
  return [...files].sort((left, right) => {
    const leftPrimary = isPrimaryDeliverable(left) ? 0 : 1;
    const rightPrimary = isPrimaryDeliverable(right) ? 0 : 1;
    if (leftPrimary !== rightPrimary) {
      return leftPrimary - rightPrimary;
    }
    const leftPdf = left.name.toLowerCase().endsWith(".pdf") ? 0 : 1;
    const rightPdf = right.name.toLowerCase().endsWith(".pdf") ? 0 : 1;
    if (leftPdf !== rightPdf) {
      return leftPdf - rightPdf;
    }
    return (right.mtime || 0) - (left.mtime || 0);
  });
}

export function linkifyArtifactNames(content: string, files: OutputFile[], sessionId?: string): string {
  if (!content || !sessionId || files.length === 0) {
    return content;
  }
  let next = content;
  sortDeliverableFiles(files).forEach((file) => {
    if (!file.name || next.includes(`](${getDownloadUrl(file.path, sessionId)})`)) {
      return;
    }
    const openUrl = getDownloadUrl(file.path, sessionId);
    next = next.replaceAll(file.name, `[${file.name}](${openUrl})`);
  });
  return next;
}

function ArtifactRow({
  compact,
  file,
  sessionId,
}: {
  compact?: boolean;
  file: OutputFile;
  sessionId?: string;
}) {
  const openUrl = getDownloadUrl(file.path, sessionId);
  const saveUrl = getDownloadUrl(file.path, sessionId, { download: true });
  const isPdf = file.name.toLowerCase().endsWith(".pdf");

  return (
    <div className={`artifact-card ${isPdf ? "artifact-card--pdf" : ""} ${compact ? "artifact-card--compact" : ""}`}>
      <span className="artifact-icon">
        <FileIcon name={file.name} />
      </span>
      <div className="artifact-copy">
        <strong title={file.name}>{file.name}</strong>
        <span>{formatBytes(file.size)}</span>
      </div>
      <div className="artifact-actions">
        <Tooltip title={isPdf ? "在浏览器中打开" : "打开"}>
          <Button
            aria-label={`打开 ${file.name}`}
            className="artifact-download"
            href={openUrl}
            rel="noreferrer"
            size={compact ? "small" : "middle"}
            target="_blank"
            type="primary"
          >
            打开
          </Button>
        </Tooltip>
        <Tooltip title="下载到本地">
          <Button
            aria-label={`下载 ${file.name}`}
            className="artifact-download"
            download={file.name}
            href={saveUrl}
            icon={<DownloadOutlined />}
            shape="circle"
            size={compact ? "small" : "middle"}
          />
        </Tooltip>
      </div>
    </div>
  );
}

interface DeliverableFilesProps {
  files: OutputFile[];
  sessionId?: string;
  variant?: "shelf" | "banner";
}

export function DeliverableFiles({ files, sessionId, variant = "shelf" }: DeliverableFilesProps) {
  const ordered = sortDeliverableFiles(files);
  if (ordered.length === 0) {
    if (variant === "banner") {
      return null;
    }
    return (
      <div className="artifact-empty">
        <FileTextOutlined aria-hidden />
        暂无输出文件
      </div>
    );
  }

  if (variant === "banner") {
    const [primary, ...rest] = ordered;
    return (
      <div className="artifact-shelf artifact-shelf--banner">
        <ArtifactRow compact file={primary} sessionId={sessionId} />
        {rest.length > 0 ? (
          <details className="deliverable-more">
            <summary>
              其余 {rest.length} 个文件
            </summary>
            <div className="deliverable-more-list">
              {rest.map((file) => (
                <ArtifactRow compact file={file} key={file.path} sessionId={sessionId} />
              ))}
            </div>
          </details>
        ) : null}
      </div>
    );
  }

  return (
    <div className="artifact-shelf">
      {ordered.map((file) => (
        <ArtifactRow file={file} key={file.path} sessionId={sessionId} />
      ))}
    </div>
  );
}
