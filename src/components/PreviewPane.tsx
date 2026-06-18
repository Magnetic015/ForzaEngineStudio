// Right-panel engine render preview: action buttons (top-right) and the live preview
// image (or a placeholder). Status notifications now live in the SystemNotice above
// the AI composer (left panel).
import { Button } from "@douyinfe/semi-ui";
import type { PreviewPaneProps } from "../types";
import RmsChart from "./RmsChart";

export default function PreviewPane(props: PreviewPaneProps) {
  const {
    previewSrc, rmsHistory, running, canStart, savedJsonPath, onOpenSaveDir, onStart, onStop,
    onImportJson, onResetPreview, injecting, canInject, onInject, onStopInject,
  } = props;
  return (
    <>
      <div className="preview-toolbar">
        {running && <RmsChart data={rmsHistory} />}
        {savedJsonPath && (
          <Button theme="light" type="tertiary" onClick={onOpenSaveDir}>
            打开保存目录
          </Button>
        )}
        <Button theme="light" type="tertiary" onClick={onImportJson} disabled={running || injecting}>
          导入 JSON
        </Button>
        <Button theme="light" type="tertiary" onClick={onResetPreview} disabled={running || injecting}>
          重置
        </Button>
        {injecting ? (
          <Button theme="solid" type="danger" onClick={onStopInject}>
            终止注入
          </Button>
        ) : (
          <Button
            theme="solid"
            type="secondary"
            onClick={onInject}
            disabled={!canInject}
            title={canInject ? "把当前设计注入到运行中的游戏" : "先渲染或导入一个设计 JSON"}
          >
            注入图层
          </Button>
        )}
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
