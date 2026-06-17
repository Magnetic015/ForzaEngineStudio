// 系统提示：显示引擎 / AI 的状态通知（原先在预览区下方的 status，现移到 AI 输入区
// 上方、即过去 last-prompt 所在的位置）。`key={status}` 在文本变化时重放滚动进场动画。
import type { SystemNoticeProps } from "../types";

export default function SystemNotice({ status }: SystemNoticeProps) {
  return (
    <div className="system-notice">
      <span key={status} className="system-notice-text" title={status}>
        {status}
      </span>
    </div>
  );
}
