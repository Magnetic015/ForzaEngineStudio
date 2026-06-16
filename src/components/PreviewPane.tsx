// Right-panel engine render preview: shows the live preview image (or a placeholder) and a status line.
import type { PreviewPaneProps } from "../types";

export default function PreviewPane(props: PreviewPaneProps) {
  const { previewSrc, status } = props;
  return (
    <>
      <div className="preview-box">
        {previewSrc ? (
          <img className="preview-img" alt="渲染预览" src={previewSrc} />
        ) : (
          <div className="placeholder">预览区 — 选择图片并点击「开始渲染」</div>
        )}
      </div>
      <div className="status">{status}</div>
    </>
  );
}
