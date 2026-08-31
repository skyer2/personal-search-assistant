import {
  PaperClipOutlined,
  PlusOutlined,
  SendOutlined,
  StopOutlined
} from "@ant-design/icons";
import { Button, Segmented, Tooltip, Upload } from "antd";
import type { UploadFile } from "antd";
import type { SearchMode, UploadedItem } from "../types";

interface ChatComposerProps {
  isAwaitingApproval?: boolean;
  isCancelling: boolean;
  isRunning: boolean;
  isUploading: boolean;
  mode: SearchMode;
  onModeChange: (mode: SearchMode) => void;
  onNewSession: () => void;
  onCancel: () => void;
  onQueryChange: (value: string) => void;
  onSubmit: () => void;
  onUpload: (items: UploadedItem[]) => Promise<void> | void;
  query: string;
  stagedItems: UploadedItem[];
  uploadedItems: UploadedItem[];
  onStagedItemsChange: (items: UploadedItem[]) => void;
}

function toUploadedItem(file: UploadFile): UploadedItem | null {
  if (!file.originFileObj) {
    return null;
  }

  return {
    uid: file.uid,
    name: file.name,
    size: file.size || 0,
    raw: file.originFileObj
  };
}

function uniqueUploadedItems(items: UploadedItem[]): UploadedItem[] {
  const names = new Set<string>();
  return items.filter((item) => {
    if (names.has(item.name)) {
      return false;
    }
    names.add(item.name);
    return true;
  });
}

export function ChatComposer({
  isAwaitingApproval = false,
  isCancelling,
  isRunning,
  isUploading,
  mode,
  onModeChange,
  onCancel,
  onNewSession,
  onQueryChange,
  onStagedItemsChange,
  onSubmit,
  onUpload,
  query,
  stagedItems,
  uploadedItems
}: ChatComposerProps) {
  const hasStagedFiles = stagedItems.length > 0;
  const canSubmit = query.trim().length > 0;

  function handleAttachmentChange(fileList: UploadFile[]) {
    const nextItems = uniqueUploadedItems(
      fileList
        .map(toUploadedItem)
        .filter((item): item is UploadedItem => Boolean(item))
    );

    if (nextItems.length === 0) {
      return;
    }

    onStagedItemsChange(nextItems);
    void Promise.resolve(onUpload(nextItems)).finally(() => {
      onStagedItemsChange([]);
    });
  }

  return (
    <section className="chat-composer" aria-label="发送搜索任务">
      <div className="composer-mode-row">
        <Segmented
          aria-label="搜索模式"
          disabled={isRunning}
          onChange={(value) => onModeChange(value as SearchMode)}
          options={[
            { label: "Auto", value: "auto" },
            { label: "Quick", value: "quick" },
            { label: "Deep", value: "deep" }
          ]}
          value={mode}
        />
      </div>

      {uploadedItems.length > 0 ? (
        <div className="attachment-strip" aria-label="当前会话附件">
          {uploadedItems.map((item) => (
            <span className="attachment-pill" key={`${item.uid}-${item.name}`}>
              <PaperClipOutlined aria-hidden />
              {item.name}
            </span>
          ))}
        </div>
      ) : null}

      {hasStagedFiles ? (
        <div className="attachment-strip" aria-label="待上传附件">
          {stagedItems.map((item) => (
            <span className="attachment-pill attachment-pill--pending" key={item.uid}>
              <PaperClipOutlined aria-hidden />
              {item.name}
            </span>
          ))}
          {isUploading ? <span className="attachment-uploading">附着中...</span> : null}
        </div>
      ) : null}

      {isAwaitingApproval ? (
        <div className="composer-pause-hint" role="status">
          任务已暂停，等待人工审批。批准或拒绝后才会继续。
        </div>
      ) : null}

      <div className="composer-shell">
        <textarea
          aria-label="搜索问题"
          disabled={isRunning}
          onChange={(event) => onQueryChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSubmit();
            }
          }}
          placeholder="向 Personal Search Assistant 提问..."
          value={query}
        />

        <div className="composer-toolbar">
          <div className="composer-left-actions">
            <Tooltip title="新建会话">
              <Button
                aria-label="新建会话"
                className="composer-icon-button"
                icon={<PlusOutlined />}
                onClick={onNewSession}
                shape="circle"
              />
            </Tooltip>
            <Upload
              beforeUpload={() => false}
              fileList={[]}
              multiple
              onChange={(info) => {
                handleAttachmentChange(info.fileList.length > 0 ? info.fileList : [info.file]);
              }}
              showUploadList={false}
            >
              <Tooltip title="选择附件">
                <Button
                  aria-label="选择附件"
                  className="composer-icon-button"
                  disabled={isRunning || isUploading}
                  icon={<PaperClipOutlined />}
                  shape="circle"
                />
              </Tooltip>
            </Upload>
          </div>

          <Tooltip title={isAwaitingApproval ? "取消已暂停的任务" : isRunning ? "取消当前任务" : "发送"}>
            <Button
              aria-label={isRunning ? "取消当前任务" : "发送任务"}
              className={isRunning ? "send-button send-button--cancel" : "send-button"}
              disabled={isRunning ? isCancelling : !canSubmit}
              icon={isRunning ? <StopOutlined /> : <SendOutlined />}
              loading={isCancelling}
              onClick={isRunning ? onCancel : onSubmit}
              shape="circle"
              type="primary"
            />
          </Tooltip>
        </div>
      </div>
    </section>
  );
}
