import { useRef, useState } from "react";
import {
  isTauri,
  readImageDataUrl,
  aiProcessImage,
  startGeneration,
  importJson,
  pickImageFile,
  pickJsonFile,
  type Cand,
  type EngineEvent,
} from "./api/tauri";
import { MODELS, type ProgressState } from "./types";
import { useEngineEvents } from "./hooks/useEngineEvents";
import { useSplitter } from "./hooks/useSplitter";
import TopBar from "./components/TopBar";
import CandidateStrip from "./components/CandidateStrip";
import AIComposer from "./components/AIComposer";
import PreviewPane from "./components/PreviewPane";

const READY_STATUS = isTauri
  ? "就绪。请选择一张本地图片。"
  : "纯前端预览模式（npm run dev）：可浏览布局与交互；渲染 / AI 需在桌面应用内运行。";

// An InputNumber cleared to empty surfaces as 0/NaN; fall back to a sane default
// at launch time (mirrors the original's parseInt(value || "3000") behaviour).
const intOrDefault = (v: number, def: number) => (Number.isFinite(v) && v > 0 ? Math.round(v) : def);

export default function App() {
  // candidate history: [原图, AI 1, AI 2, ...]; selectedIndex is the 目标图.
  const [candidates, setCandidates] = useState<Cand[]>([]);
  // Mirror of `candidates` for async paths: an AI edit resolves 10–40s later, by
  // which time the render closure may be stale — the original used a live array.
  const candidatesRef = useRef<Cand[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(-1);
  const [currentModel, setCurrentModel] = useState<string>(MODELS[0]);
  const [running, setRunningState] = useState(false);
  // Synchronous mirror of `running`: async engine events read a render closure
  // that may predate the setRunning(true) commit, so an `exit` arriving in that
  // window would otherwise be dropped (controls stuck disabled). The ref tracks
  // the live value, matching the original's synchronous module-global flag.
  const runningRef = useRef(false);
  const setRunning = (on: boolean) => {
    runningRef.current = on;
    setRunningState(on);
  };
  const [aiRunning, setAiRunning] = useState(false);

  // top-bar controls
  const [stopAt, setStopAt] = useState(3000);
  const [canvasWidth, setCanvasWidth] = useState(1000);
  const [canvasHeight, setCanvasHeight] = useState(1000);
  const [stickerMode, setStickerMode] = useState("default");
  const [bgColor, setBgColor] = useState("#ffffff");
  const [backend, setBackend] = useState("gpu");
  const [assistMode, setAssistMode] = useState("off");

  // AI composer
  const [apiKey, setApiKey] = useState("");
  const [prompt, setPrompt] = useState("");

  // render / status surfaces
  const [status, setStatus] = useState(READY_STATUS);
  const [backendText, setBackendText] = useState("");
  const [progress, setProgress] = useState<ProgressState>({ n: 0, total: 0, rms: 0 });
  const [previewSrc, setPreviewSrc] = useState("");

  const { leftPct, dragging, bodyRef, onMouseDown } = useSplitter(50);

  const selectedCand = selectedIndex >= 0 && selectedIndex < candidates.length ? candidates[selectedIndex] : null;
  const currentRenderPath = selectedCand?.path ?? null;
  const hasTarget = selectedIndex >= 0;
  const canStart = hasTarget && !running;
  const sendDisabled = aiRunning || running || !hasTarget || !apiKey.trim() || !prompt.trim();

  // ── candidate helpers ───────────────────────────────────────────────────────
  const resetCandidates = (first: Cand) => {
    const next = [first];
    candidatesRef.current = next;
    setCandidates(next);
    setSelectedIndex(0);
  };
  const addCandidate = (c: Cand) => {
    const idx = candidatesRef.current.length; // new item lands at the current length
    const next = [...candidatesRef.current, c];
    candidatesRef.current = next;
    setCandidates(next);
    setSelectedIndex(idx);
  };

  // ── actions ─────────────────────────────────────────────────────────────────
  async function pickImage() {
    if (running) return;
    if (isTauri) {
      const file = await pickImageFile();
      if (!file) return;
      setStatus("正在载入图片…");
      let u = "";
      try {
        u = await readImageDataUrl(file);
      } catch {
        /* leave placeholder */
      }
      resetCandidates({ path: file, src: u, label: "原图" });
      setStatus("已载入原图（候选①）。可做 AI 处理，或选中目标图后开始渲染。");
      return;
    }
    // plain-browser fallback: preview only
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.onchange = () => {
      const f = input.files?.[0];
      if (!f) return;
      resetCandidates({ path: null, src: URL.createObjectURL(f), label: "原图" });
      setStatus("纯前端预览模式：已加载原图。渲染 / AI 需在桌面应用内运行。");
    };
    input.click();
  }

  async function aiProcess() {
    if (aiRunning || running || !hasTarget) return;
    const key = apiKey.trim();
    const text = prompt.trim();
    if (!key || !text) return;
    const inputPath = currentRenderPath;
    if (!isTauri || !inputPath) {
      setStatus("纯前端预览模式：AI 处理需在桌面应用内运行。");
      return;
    }
    const srcLabel = selectedCand?.label || "目标图";
    setAiRunning(true);
    setStatus(`AI 处理中…（输入：${srcLabel}，可能需要十几秒）`);
    try {
      const newPath = await aiProcessImage({ image: inputPath, apiKey: key, model: currentModel, prompt: text });
      const label = "AI " + candidatesRef.current.length; // 原图 is index 0 → first AI = "AI 1"
      let u = "";
      try {
        u = await readImageDataUrl(newPath);
      } catch {
        /* leave placeholder */
      }
      addCandidate({ path: newPath, src: u, label });
      setStatus(`AI 处理完成（基于${srcLabel}），已加入候选「${label}」并选为目标图。`);
    } catch (e) {
      setStatus("AI 处理失败：" + e);
    } finally {
      setAiRunning(false);
    }
  }

  async function start() {
    if (running) return;
    const src = currentRenderPath;
    if (!isTauri || !src) {
      setStatus("纯前端预览模式：渲染需在桌面应用内运行。");
      return;
    }
    const sticker = stickerMode === "sticker";
    const assist = assistMode === "on";
    const safeStopAt = intOrDefault(stopAt, 3000);
    const safeW = intOrDefault(canvasWidth, 1000);
    const safeH = intOrDefault(canvasHeight, 1000);
    setRunning(true);
    setStatus(
      `正在启动引擎…（目标图：${selectedCand?.label || ""}，画布 ${safeW}×${safeH}${assist ? " · 模型协助" : ""}）`
    );
    setProgress({ n: 0, total: safeStopAt, rms: 0 });
    try {
      await startGeneration({
        image: src,
        stopAt: safeStopAt,
        canvasWidth: safeW,
        canvasHeight: safeH,
        sticker,
        backend,
        assist,
        bgColor,
      });
    } catch (e) {
      setStatus("启动失败：" + e);
      setRunning(false);
    }
  }

  async function handleImportJson() {
    if (running) return;
    if (!isTauri) {
      setStatus("纯前端预览模式：导入 JSON 需在桌面应用内运行。");
      return;
    }
    const file = await pickJsonFile();
    if (!file) return;
    setStatus("正在渲染 JSON…");
    try {
      const dataUrl = await importJson(file);
      setPreviewSrc(dataUrl);
      const name = file.replace(/\\/g, "/").split("/").pop() || file;
      setStatus(`已导入并在预览区渲染 JSON：${name}`);
    } catch (e) {
      setStatus("导入 JSON 失败：" + e);
    }
  }

  // ── engine event stream ───────────────────────────────────────────────────────
  useEngineEvents((p: EngineEvent) => {
    switch (p.type) {
      case "meta":
        setStatus(`画布 ${p.width}×${p.height} · 生成中…`);
        break;
      case "backend":
        setBackendText("计算后端：" + p.message);
        break;
      case "assist": {
        const parts = Object.keys(p.applied || {});
        if (parts.length) setStatus(`模型协助已启用：${parts.join(" · ")} · 生成中…`);
        break;
      }
      case "progress":
        setProgress({ n: p.shape_count, total: p.total, rms: p.rms });
        break;
      case "frame":
        if (p.png) setPreviewSrc("data:image/png;base64," + p.png);
        setProgress({ n: p.shape_count, total: p.total, rms: p.rms });
        break;
      case "done":
        if (p.png) setPreviewSrc("data:image/png;base64," + p.png);
        setProgress({ n: p.shape_count, total: p.total ?? p.shape_count, rms: p.rms });
        setStatus(
          `完成！共 ${p.shape_count} 个形状，最终 RMS ${typeof p.rms === "number" ? p.rms.toFixed(2) : p.rms} · JSON 已保存：${p.json_path}`
        );
        setRunning(false);
        break;
      case "error":
        setStatus("错误：" + p.message);
        setRunning(false);
        break;
      case "exit":
        if (runningRef.current) {
          setStatus("引擎进程异常退出（code " + p.code + "）。请查看控制台日志。");
          setRunning(false);
        }
        break;
      case "log":
        console.log("[sidecar]", p.message);
        break;
    }
  });

  const targetSrc = selectedCand?.src || "";

  return (
    <main className="app">
      <header className="topbar">
        <TopBar
          stopAt={stopAt}
          setStopAt={setStopAt}
          canvasWidth={canvasWidth}
          setCanvasWidth={setCanvasWidth}
          canvasHeight={canvasHeight}
          setCanvasHeight={setCanvasHeight}
          stickerMode={stickerMode}
          setStickerMode={setStickerMode}
          bgColor={bgColor}
          setBgColor={setBgColor}
          backend={backend}
          setBackend={setBackend}
          assistMode={assistMode}
          setAssistMode={setAssistMode}
          backendText={backendText}
          progress={progress}
          running={running}
          canStart={canStart}
          onStart={start}
          onImportJson={handleImportJson}
        />
      </header>

      <div className="body" ref={bodyRef}>
        <section className="left" style={{ flex: `0 0 ${leftPct}%` }}>
          <div className="images">
            <CandidateStrip
              candidates={candidates}
              selectedIndex={selectedIndex}
              onSelect={setSelectedIndex}
              onPick={pickImage}
            />
            <figure className="target-fig">
              <div className="target">
                <img alt="目标图" src={targetSrc || undefined} />
              </div>
            </figure>
          </div>

          <div className="ai-block">
            <AIComposer
              prompt={prompt}
              setPrompt={setPrompt}
              onSend={aiProcess}
              sendDisabled={sendDisabled}
              apiKey={apiKey}
              setApiKey={setApiKey}
              model={currentModel}
              onSelectModel={setCurrentModel}
            />
          </div>
        </section>

        <div
          className={`splitter${dragging ? " dragging" : ""}`}
          role="separator"
          aria-orientation="vertical"
          aria-label="拖动调整左右面板宽度"
          onMouseDown={onMouseDown}
        >
          <span className="splitter-handle">
            <svg
              width="12"
              height="16"
              viewBox="0 0 12 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M5 3.5 L2 8 L5 12.5 M7 3.5 L10 8 L7 12.5" />
            </svg>
          </span>
        </div>

        <section className="right">
          <PreviewPane previewSrc={previewSrc} status={status} />
        </section>
      </div>
    </main>
  );
}
