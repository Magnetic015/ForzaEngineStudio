import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";

// True inside the Tauri webview; false when served as a plain page (`npm run dev`).
export const isTauri = "__TAURI_INTERNALS__" in window;

// One candidate image. `path`: real file (Tauri side); `src`: data/object URL for <img>.
export interface Cand {
  path: string | null;
  src: string;
  label: string;
}

export interface StartParams {
  image: string;
  stopAt: number;
  canvasWidth: number;
  canvasHeight: number;
  sticker: boolean;
  backend: string;
  assist: boolean;
  bgColor: string;
}

// `engine-event` payloads — line-JSON the Rust side forwards verbatim from the
// Python sidecar. Keys mirror the sidecar's JSON exactly (snake_case).
export type EngineEvent =
  | { type: "meta"; width: number; height: number }
  | { type: "backend"; message: string }
  | { type: "assist"; applied?: Record<string, unknown> }
  | { type: "progress"; shape_count: number; total: number; rms: number }
  | { type: "frame"; shape_count: number; total: number; rms: number; png?: string }
  | { type: "done"; shape_count: number; total?: number; rms: number; png?: string; json_path: string }
  | { type: "error"; message: string }
  | { type: "exit"; code: number | null; gen?: number }
  | { type: "log"; message: string };

// ── Tauri command wrappers ────────────────────────────────────────────────────
// NOTE: keys stay camelCase here; Tauri auto-maps them to the Rust snake_case
// params (stopAt → stop_at, apiKey → api_key, jsonPath → json_path, ...).
export const readImageDataUrl = (path: string) =>
  invoke<string>("read_image_data_url", { path });

export const aiProcessImage = (args: { image: string; apiKey: string; model: string; prompt: string }) =>
  invoke<string>("ai_process_image", args);

export const startGeneration = (p: StartParams) => invoke<number>("start_generation", { ...p });

export const stopGeneration = () => invoke("stop_generation");

export const importJson = (jsonPath: string) => invoke<string>("import_json", { jsonPath });

// Subscribe to the engine event stream. Resolves to an unlisten fn.
export const listenEngine = (cb: (e: EngineEvent) => void): Promise<UnlistenFn> =>
  listen("engine-event", (e) => cb(e.payload as EngineEvent));

// ── dialog helpers ────────────────────────────────────────────────────────────
export async function pickImageFile(): Promise<string | null> {
  const f = await open({
    multiple: false,
    directory: false,
    filters: [{ name: "图片", extensions: ["png", "jpg", "jpeg", "webp", "bmp", "gif"] }],
  });
  return typeof f === "string" ? f : null;
}

export async function pickJsonFile(): Promise<string | null> {
  const f = await open({
    multiple: false,
    directory: false,
    filters: [{ name: "FD6 形状 JSON", extensions: ["json"] }],
  });
  return typeof f === "string" ? f : null;
}
