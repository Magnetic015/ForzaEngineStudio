import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";

// True inside the Tauri webview; false when served as a plain page (`npm run dev`).
const isTauri = "__TAURI_INTERNALS__" in window;

interface Cand { path: string | null; src: string; label: string; } // path: file (Tauri); src: data/object URL
let candidates: Cand[] = []; // [原图, AI 1, AI 2, ...] — every AI result is cached here
let selectedIndex = -1;      // the "目标图" (render + next-AI input)
let currentModel = "gemini-3.1-flash-image";
let running = false;
let aiRunning = false;

const $ = (id: string) => document.getElementById(id);

function setStatus(text: string) {
  const el = $("status");
  if (el) el.textContent = text;
}

function setProgress(n: number, total: number, rms: number) {
  const pct = total > 0 ? Math.min(100, (n / total) * 100) : 0;
  const bar = $("bar") as HTMLDivElement | null;
  if (bar) bar.style.width = pct.toFixed(1) + "%";
  const lbl = $("progress-label");
  const rmsTxt = typeof rms === "number" ? rms.toFixed(2) : String(rms);
  if (lbl) lbl.textContent = `${n} / ${total} 形状 · RMS ${rmsTxt} · ${pct.toFixed(0)}%`;
}

function selectedCand(): Cand | null {
  return selectedIndex >= 0 && selectedIndex < candidates.length ? candidates[selectedIndex] : null;
}
const currentRenderPath = () => selectedCand()?.path ?? null;
const hasTarget = () => selectedIndex >= 0;

function setRunning(on: boolean) {
  running = on;
  const start = $("start-btn") as HTMLButtonElement | null;
  const imp = $("import-json-btn") as HTMLButtonElement | null;
  if (start) start.disabled = on || !hasTarget();
  if (imp) imp.disabled = on;
  updateAiBtn();
}

function updateAiBtn() {
  const btn = $("ai-btn") as HTMLButtonElement | null;
  const key = (($("ai-key") as HTMLInputElement | null)?.value || "").trim();
  const prompt = (($("ai-prompt") as HTMLTextAreaElement | null)?.value || "").trim();
  if (btn) btn.disabled = aiRunning || running || !hasTarget() || !key || !prompt;
}

// ── candidate strip + target image ───────────────────────────────────────────
function renderCandidates() {
  const strip = $("candidates");
  if (!strip) return;
  strip.replaceChildren();
  if (candidates.length === 0) {
    // empty state: a placeholder slot that opens the file picker on click
    const btn = document.createElement("button");
    btn.className = "cand cand-empty";
    btn.title = "点击选择本地图片";
    const box = document.createElement("span");
    box.className = "cand-thumb";
    box.textContent = "+";
    btn.appendChild(box);
    btn.addEventListener("click", () => pickImage());
    strip.appendChild(btn);
    return;
  }
  candidates.forEach((c, i) => {
    const btn = document.createElement("button");
    btn.className = "cand" + (i === selectedIndex ? " is-selected" : "");
    btn.title = c.label;
    const box = document.createElement("span");
    box.className = "cand-thumb";
    const img = document.createElement("img");
    img.src = c.src;
    img.alt = c.label;
    box.appendChild(img);
    btn.appendChild(box); // label removed; c.label kept as the title tooltip above
    btn.addEventListener("click", () => selectCandidate(i));
    strip.appendChild(btn);
  });
}

function setTargetImage(src: string) {
  const t = $("target-img") as HTMLImageElement | null;
  if (!t) return;
  if (src) t.src = src; else t.removeAttribute("src");
}

function selectCandidate(i: number) {
  if (i < 0 || i >= candidates.length) return;
  selectedIndex = i;
  setTargetImage(candidates[i].src);
  renderCandidates();
  const start = $("start-btn") as HTMLButtonElement | null;
  if (start) start.disabled = running || !hasTarget();
  updateAiBtn();
}

function resetCandidates(first: Cand) {
  candidates = [first];
  selectedIndex = -1;
  selectCandidate(0);
}

function addCandidate(c: Cand) {
  candidates.push(c);
  selectCandidate(candidates.length - 1);
}

// ── model selector popup ─────────────────────────────────────────────────────
function popEl() { return $("model-pop") as HTMLDivElement | null; }
function isPopOpen() { return !!popEl() && !popEl()!.hasAttribute("hidden"); }
function setPopOpen(open: boolean) {
  const pop = popEl();
  if (!pop) return;
  if (open) pop.removeAttribute("hidden"); else pop.setAttribute("hidden", "");
  $("model-trigger")?.setAttribute("aria-expanded", String(open));
}
function selectModel(model: string) {
  currentModel = model;
  const label = $("model-label");
  if (label) label.textContent = model;
  document.querySelectorAll<HTMLElement>(".popitem").forEach((el) => {
    el.classList.toggle("is-selected", el.dataset.model === model);
  });
}

// ── actions ──────────────────────────────────────────────────────────────────
async function pickImage() {
  if (running) return;
  if (isTauri) {
    const file = await open({
      multiple: false,
      directory: false,
      filters: [{ name: "图片", extensions: ["png", "jpg", "jpeg", "webp", "bmp", "gif"] }],
    });
    if (typeof file === "string") {
      const name = file.replace(/\\/g, "/").split("/").pop() || file;
      const fileEl = $("file-name");
      if (fileEl) fileEl.textContent = name;
      setStatus("正在载入图片…");
      let u = "";
      try { u = await invoke<string>("read_image_data_url", { path: file }); } catch { /* leave placeholder */ }
      resetCandidates({ path: file, src: u, label: "原图" });
      setRunning(false);
      setStatus("已载入原图（候选①）。可做 AI 处理，或选中目标图后开始渲染。");
    }
    return;
  }
  // plain-browser fallback: preview only
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/*";
  input.onchange = () => {
    const f = input.files?.[0];
    if (!f) return;
    const fileEl = $("file-name");
    if (fileEl) fileEl.textContent = f.name + "（预览）";
    resetCandidates({ path: null, src: URL.createObjectURL(f), label: "原图" });
    setRunning(false);
    setStatus("纯前端预览模式：已加载原图。渲染 / AI 需在桌面应用内运行。");
  };
  input.click();
}

async function aiProcess() {
  if (aiRunning || running || !hasTarget()) return;
  const key = (($("ai-key") as HTMLInputElement).value || "").trim();
  const prompt = (($("ai-prompt") as HTMLTextAreaElement).value || "").trim();
  if (!key || !prompt) return;
  const inputPath = currentRenderPath();
  if (!isTauri || !inputPath) {
    setStatus("纯前端预览模式：AI 处理需在桌面应用内运行。");
    return;
  }
  const srcLabel = selectedCand()?.label || "目标图";
  aiRunning = true;
  updateAiBtn();
  setStatus(`AI 处理中…（输入：${srcLabel}，可能需要十几秒）`);
  try {
    // AI processes the currently selected target image; the result is cached as
    // a new candidate so the full history is browsable.
    const newPath = await invoke<string>("ai_process_image", { image: inputPath, apiKey: key, model: currentModel, prompt });
    const label = "AI " + candidates.length; // 原图 is index 0 → first AI = "AI 1"
    let u = "";
    try { u = await invoke<string>("read_image_data_url", { path: newPath }); } catch { /* leave placeholder */ }
    addCandidate({ path: newPath, src: u, label });
    setStatus(`AI 处理完成（基于${srcLabel}），已加入候选「${label}」并选为目标图。`);
  } catch (e) {
    setStatus("AI 处理失败：" + e);
  } finally {
    aiRunning = false;
    updateAiBtn();
  }
}

async function start() {
  if (running) return;
  const src = currentRenderPath();
  if (!isTauri || !src) {
    setStatus("纯前端预览模式：渲染需在桌面应用内运行。");
    return;
  }
  const stopAt = parseInt(($("stop-at") as HTMLInputElement).value || "3000", 10);
  const canvasWidth = parseInt(($("canvas-w") as HTMLInputElement).value || "1000", 10);
  const canvasHeight = parseInt(($("canvas-h") as HTMLInputElement).value || "1000", 10);
  const sticker = (($("sticker-mode") as HTMLSelectElement | null)?.value || "default") === "sticker";
  const backend = ($("backend-select") as HTMLSelectElement | null)?.value || "gpu";
  const assist = (($("assist-mode") as HTMLSelectElement | null)?.value || "off") === "on";
  setRunning(true);
  setStatus(`正在启动引擎…（目标图：${selectedCand()?.label || ""}，画布 ${canvasWidth}×${canvasHeight}${assist ? " · 模型协助" : ""}）`);
  setProgress(0, stopAt, 0);
  try {
    await invoke("start_generation", { image: src, stopAt, canvasWidth, canvasHeight, sticker, backend, assist });
  } catch (e) {
    setStatus("启动失败：" + e);
    setRunning(false);
  }
}

async function importJson() {
  if (running) return;
  if (!isTauri) {
    setStatus("纯前端预览模式：导入 JSON 需在桌面应用内运行。");
    return;
  }
  const file = await open({
    multiple: false,
    directory: false,
    filters: [{ name: "FD6 形状 JSON", extensions: ["json"] }],
  });
  if (typeof file !== "string") return;
  setStatus("正在渲染 JSON…");
  try {
    const dataUrl = await invoke<string>("import_json", { jsonPath: file });
    const img = $("preview") as HTMLImageElement | null;
    if (img) img.src = dataUrl;
    const name = file.replace(/\\/g, "/").split("/").pop() || file;
    setStatus(`已导入并在预览区渲染 JSON：${name}`);
  } catch (e) {
    setStatus("导入 JSON 失败：" + e);
  }
}

function handleEvent(p: any) {
  if (!p || typeof p !== "object") return;
  const img = $("preview") as HTMLImageElement | null;
  switch (p.type) {
    case "meta":
      setStatus(`画布 ${p.width}×${p.height} · 生成中…`);
      break;
    case "backend": {
      const b = $("backend");
      if (b) b.textContent = "计算后端：" + p.message;
      break;
    }
    case "assist": {
      const parts = Object.keys(p.applied || {});
      if (parts.length) setStatus(`模型协助已启用：${parts.join(" · ")} · 生成中…`);
      break;
    }
    case "progress":
      setProgress(p.shape_count, p.total, p.rms);
      break;
    case "frame":
      if (img && p.png) img.src = "data:image/png;base64," + p.png;
      setProgress(p.shape_count, p.total, p.rms);
      break;
    case "done":
      if (img && p.png) img.src = "data:image/png;base64," + p.png;
      setProgress(p.shape_count, p.total ?? p.shape_count, p.rms);
      setStatus(`完成！共 ${p.shape_count} 个形状，最终 RMS ${typeof p.rms === "number" ? p.rms.toFixed(2) : p.rms} · JSON 已保存：${p.json_path}`);
      setRunning(false);
      break;
    case "error":
      setStatus("错误：" + p.message);
      setRunning(false);
      break;
    case "exit":
      if (running) {
        setStatus("引擎进程异常退出（code " + p.code + "）。请查看控制台日志。");
        setRunning(false);
      }
      break;
    case "log":
      console.log("[sidecar]", p.message);
      break;
  }
}

// Draggable splitter: resize the left panel (right flexes to fill). Width is
// stored as a percentage so it survives window resizes; each side is clamped to
// a minimum of 30% of the window width.
function setupSplitter() {
  const splitter = $("splitter");
  const body = document.querySelector(".body") as HTMLElement | null;
  const left = document.querySelector(".left") as HTMLElement | null;
  if (!splitter || !body || !left) return;
  let dragging = false;
  const apply = (clientX: number) => {
    const rect = body.getBoundingClientRect();
    if (rect.width <= 0) return;
    let pct = ((clientX - rect.left) / rect.width) * 100;
    pct = Math.max(30, Math.min(70, pct));
    left.style.flex = `0 0 ${pct.toFixed(2)}%`;
  };
  splitter.addEventListener("mousedown", (e) => {
    dragging = true;
    splitter.classList.add("dragging");
    document.body.style.userSelect = "none";
    (e as MouseEvent).preventDefault();
  });
  document.addEventListener("mousemove", (e) => { if (dragging) apply((e as MouseEvent).clientX); });
  document.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove("dragging");
    document.body.style.userSelect = "";
  });
}

window.addEventListener("DOMContentLoaded", () => {
  $("pick-btn")?.addEventListener("click", pickImage);
  $("start-btn")?.addEventListener("click", start);
  $("import-json-btn")?.addEventListener("click", importJson);
  $("ai-btn")?.addEventListener("click", aiProcess);
  $("ai-key")?.addEventListener("input", updateAiBtn);
  $("ai-prompt")?.addEventListener("input", updateAiBtn);

  // textarea: Enter sends, Shift+Enter newline
  $("ai-prompt")?.addEventListener("keydown", (e) => {
    const ke = e as KeyboardEvent;
    if (ke.key === "Enter" && !ke.shiftKey) { ke.preventDefault(); aiProcess(); }
  });

  // model selector popup
  $("model-trigger")?.addEventListener("click", () => setPopOpen(!isPopOpen()));
  document.querySelectorAll<HTMLElement>(".popitem").forEach((el) => {
    el.addEventListener("click", () => { if (el.dataset.model) selectModel(el.dataset.model); setPopOpen(false); });
  });
  document.addEventListener("click", (e) => {
    if (!isPopOpen()) return;
    const t = e.target as Node;
    if (popEl()?.contains(t) || $("model-trigger")?.contains(t)) return;
    setPopOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (!isPopOpen()) return;
    if (e.key === "Escape") { setPopOpen(false); return; }
    const items = Array.from(document.querySelectorAll<HTMLElement>(".popitem"));
    const idx = ["1", "2", "3"].indexOf(e.key);
    if (idx >= 0 && items[idx]?.dataset.model) { selectModel(items[idx].dataset.model!); setPopOpen(false); }
  });

  if (isTauri) {
    listen("engine-event", (e) => handleEvent(e.payload as any));
  } else {
    setStatus("纯前端预览模式（npm run dev）：可浏览布局与交互；渲染 / AI 需在桌面应用内运行。");
  }

  setupSplitter();
  selectModel(currentModel);
  renderCandidates();
  setRunning(false);
});
