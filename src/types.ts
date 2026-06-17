import type { Cand } from "./api/tauri";

// The three selectable AI image-edit models (label === value sent to the sidecar).
export const MODELS = [
  "gemini-3.1-flash-image",
  "plus/gpt-image-2",
  "grok-imagine-image-lite",
] as const;

export interface ProgressState {
  n: number;
  total: number;
  rms: number;
}

// One sample of the live RMS curve: committed-shape count + the RMS at that point.
export interface RmsPoint {
  n: number;
  rms: number;
}

// ── Component prop contracts (frozen here; App + leaves implement against them) ──

export interface TopBarProps {
  stopAt: number;
  setStopAt: (v: number) => void;
  quality: number;
  setQuality: (n: number) => void;
  canvasWidth: number;
  setCanvasWidth: (v: number) => void;
  canvasHeight: number;
  setCanvasHeight: (v: number) => void;
  stickerMode: string; // "sticker" | "default"
  setStickerMode: (v: string) => void;
  bgColor: string;
  setBgColor: (v: string) => void;
  backend: string; // "gpu" | "cpu" | "auto"
  setBackend: (v: string) => void;
  assistMode: string; // "off" | "on"
  setAssistMode: (v: string) => void;
  progress: ProgressState;
}

export interface CandidateStripProps {
  candidates: Cand[];
  selectedIndex: number;
  onSelect: (i: number) => void;
  onPick: () => void; // open the file picker (empty-state "+" slot)
  disabled: boolean; // AI editing or rendering in flight → lock selection + picker
}

export interface AIComposerProps {
  onSend: (text: string) => void;
  aiRunning: boolean; // drives AIChatInput `generating` (clears input + shows stop)
  sendBlocked: boolean; // no key / no target / busy → can't send
  // model + key are configured inside the AIChatInput configure area:
  apiKey: string;
  setApiKey: (v: string) => void;
  model: string;
  onSelectModel: (m: string) => void;
}

// 系统提示组件：展示引擎 / AI 的状态通知文本。
export interface SystemNoticeProps {
  status: string;
}

export interface CropModalProps {
  visible: boolean;
  src: string; // data/object URL of the target image to crop ("" → nothing to crop)
  saving: boolean; // persisting the crop → show the OK button's loading state
  onCancel: () => void;
  onSave: (dataUrl: string) => void; // cropped PNG as a data URL
}

export interface PreviewPaneProps {
  previewSrc: string; // "" → show placeholder
  rmsHistory: RmsPoint[]; // live RMS-vs-shape-count samples for the toolbar sparkline
  // action buttons live in the preview pane's top-right corner
  running: boolean;
  canStart: boolean; // a target image is selected and not running
  savedJsonPath: string; // last render's shape JSON path ("" → hide 打开保存目录)
  onOpenSaveDir: () => void; // open the folder containing savedJsonPath
  onStart: () => void;
  onStop: () => void; // terminate the in-flight render
  onImportJson: () => void;
  onResetPreview: () => void;
}
