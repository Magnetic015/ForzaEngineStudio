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

// ── Component prop contracts (frozen here; App + leaves implement against them) ──

export interface TopBarProps {
  stopAt: number;
  setStopAt: (v: number) => void;
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
  running: boolean;
  canStart: boolean; // a target image is selected and not running
  onStart: () => void;
  onStop: () => void; // terminate the in-flight render
  onImportJson: () => void;
  onResetPreview: () => void;
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
  lastPrompt: string; // most recent submitted prompt, echoed above the input
  // model + key are configured inside the AIChatInput configure area:
  apiKey: string;
  setApiKey: (v: string) => void;
  model: string;
  onSelectModel: (m: string) => void;
}

export interface PreviewPaneProps {
  previewSrc: string; // "" → show placeholder
  status: string;
}
