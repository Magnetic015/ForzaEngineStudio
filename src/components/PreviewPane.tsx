// Right-panel engine render preview: action buttons (top-right) and the live preview
// image (or a placeholder). Status notifications now live in the SystemNotice above
// the AI composer (left panel).
import { Button } from "@douyinfe/semi-ui";
import type { PreviewPaneProps } from "../types";
import RmsChart from "./RmsChart";

export default function PreviewPane(props: PreviewPaneProps) {
  const { previewSrc, rmsHistory, running, canStart, savedJsonPath, onOpenSaveDir, onStart, onStop, onImportJson, onResetPreview } =
    props;
  return (
    <>
      <div className="preview-toolbar">
        {running && <RmsChart data={rmsHistory} />}
        {savedJsonPath && (
          <Button theme="light" type="tertiary" onClick={onOpenSaveDir}>
            打开保存目录
          </Button>
        )}
        <Button theme="light" type="tertiary" onClick={onImportJson} disabled={running}>
          导入 JSON
        </Button>
        <Button theme="light" type="tertiary" onClick={onResetPreview} disabled={running}>
          重置
        </Button>
        {running ? (
          <Button theme="solid" type="danger" onClick={onStop}>
            终止渲染
          </Button>
        ) : (
          <Button theme="solid" type="primary" onClick={onStart} disabled={!canStart}>
            开始渲染
          </Button>
        )}
      </div>
      <div className="preview-box">
        {previewSrc ? (
          <img className="preview-img" alt="渲染预览" src={previewSrc} />
        ) : (
          <div className="placeholder">预览区 — 选择图片并点击「开始渲染」</div>
        )}
      </div>
    </>
  );
}
