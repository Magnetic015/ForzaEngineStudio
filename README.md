# Forza Engine Studio

**[English](#english) · [中文](#中文)**

> **Credits / 致谢**
> This project is a refactor built on **[ForzaDesigner6](https://github.com/tokyubevoxelverse/ForzaDesigner6)** — its geometric shape‑generation engine (`shapegen` + `io`) is vendored from that project (PySide6/Qt stripped, run as a Python sidecar). Huge thanks to the original author for open‑sourcing it. 🙏
> 本项目基于 **[ForzaDesigner6](https://github.com/tokyubevoxelverse/ForzaDesigner6)** 改造——其几何形状生成引擎（`shapegen` + `io`）vendoring 自该项目（已剥离 PySide6/Qt，作为 Python sidecar 运行）。感谢开源作者！🙏

---

## English

Forza Engine Studio is a desktop app that approximates any local image with a set of **translucent geometric shapes** (rotated ellipses) and renders it **in real time** — you watch the canvas fill in shape by shape.

### Features

- **Real‑time shape rendering** — image → translucent rotated‑ellipse shapes, streamed frame by frame to the preview.
- **Resizable 2‑pane layout** — top controls + a left panel (candidate strip + target image) + a right live‑preview pane, separated by a **draggable splitter** (`<>` handle, each side clamped to ≥ 30 % of the window).
- **Controllable canvas size (W × H)** — the image is fit into your chosen canvas (aspect preserved, centered, 8 % buffer ring).
- **AI pre‑processing (optional)** — edit the source image with a third‑party image model **before** rendering, via an OpenAI‑compatible gateway:
  - `gpt-image-2` (`/images/edits`)
  - `gemini-3.1-flash-image` (`/chat/completions`)
  - `grok-imagine-image-lite` (`/chat/completions`)
  - AI runs on the **currently selected** image and each result is cached as a new candidate, so you can iterate.
- **Candidate history** — the left strip holds the original plus every AI result; click any thumbnail to make it the **target** that gets rendered.
- **Import JSON** — load a saved `*_engine.json` shape document and render it straight to the preview (no re‑generation).
- **Render modes** — default / sticker (preserve transparency).
- **Compute backend** — GPU (cross‑vendor **OpenCL**, all vendors) / CPU / auto, with graceful fallback to CPU if the GPU is unavailable.

### Architecture

```
┌────────────────────────────────────────────────────────────┐
│ Frontend (index.html / src/main.ts): controls · candidate    │
│   strip · target · AI popup · live preview · splitter         │
└───────────────┬──────────────────────────────────────────────┘
   invoke        │   ▲ listen("engine-event")
                 ▼   │
┌────────────────────────────────────────────────────────────┐
│ Rust backend (src-tauri/src/lib.rs): commands                 │
│   start_generation · ai_process_image · read_image_data_url   │
│   · import_json — spawns sidecars, bridges line‑JSON ↔ events │
└───────────────┬──────────────────────────────────────────────┘
   argv          │   ▲ stdout: line‑JSON (meta/progress/frame/done/…)
                 ▼   │
┌────────────────────────────────────────────────────────────┐
│ Python sidecars (python/):                                    │
│   sidecar.py      → fd6.shapegen.Engine.run() (+ preprocess)  │
│   image_process.py→ AI image edit via the gateway             │
│   render_json.py  → render an existing shape JSON             │
│ Engine vendored from ForzaDesigner6 (fd6/shapegen + fd6/io)   │
└────────────────────────────────────────────────────────────┘
```

### Setup (dev mode)

Prerequisites: **Rust**, **Node 18+**, **Python 3.10+**.

```powershell
# 1) Python engine deps (one‑time); requests is used by the AI sidecar
cd python
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install numpy pillow requests
cd ..

# 2) Frontend deps (one‑time)
npm install

# 3) Launch (Vite frontend + Rust backend + app window)
npm run tauri dev
```

To preview just the web front‑end (no engine/AI): `npm run dev` then open `http://localhost:1420`.

### Usage

1. Click the dashed **`+`** slot in the left candidate strip to pick a local image (it becomes candidate ① "original").
2. *(Optional)* **AI**: type an instruction in the composer, open the model popup (click the model name) to set your **API key** and pick a model, then press **Enter** (or the ↵ button). The result is cached as a new candidate and auto‑selected.
3. Click any thumbnail in the strip to choose the **target** (original or an AI result).
4. Set **canvas W × H**, **target shape count**, **render mode**, **compute backend**, then **开始渲染 / Start**. The right pane fills in live; a `*_engine.json` (FD6 shape document) is saved next to the source image on completion.
5. **导入 JSON / Import JSON** (top bar): load an existing `*_engine.json` to render it in the preview without re‑generating.

### sidecar protocol (one JSON object per stdout line)

| type | fields | meaning |
|---|---|---|
| `meta` | width, height | processed canvas size |
| `backend` | message | resolved compute backend |
| `progress` | shape_count, total, rms | lightweight progress |
| `frame` | + png (base64) | preview frame |
| `done` | + png, json_path | finished + saved JSON path |
| `error` | message | failure |

### Tech stack

Tauri 2 (Rust shell) · Vite + TypeScript (vanilla front‑end) · Python sidecars (numpy + pillow + requests) · engine vendored from **ForzaDesigner6**.

### Known limitations

- **Dev‑mode only** — the sidecar path is resolved via `CARGO_MANIFEST_DIR`; packaging (PyInstaller + Tauri resources) is a TODO.
- **AI key handling** — the key is entered at runtime and passed to the sidecar via argv; it is **never persisted to disk or logs**, but is briefly visible to local process listing. Passing it via stdin is a planned hardening.
- `grok-imagine-image-lite` may return `503 (no channel)` at the gateway depending on upstream availability.

---

## 中文

Forza Engine Studio 是一个桌面应用，把任意本地图片用一组**半透明几何形状**（旋转椭圆）逼近，并**实时**渲染——你能看着画布被一个个形状逐步"画"出来。

### 功能

- **实时形状渲染**——图像 → 半透明旋转椭圆，逐帧流式推到预览区。
- **可调双栏布局**——顶部主功能区 + 左侧面板（候选条 + 目标图）+ 右侧实时预览，中间是**可拖动分隔条**（`<>` 手柄，左右各限制 ≥ 窗口宽度的 30%）。
- **可控画布大小（宽 × 高）**——图像按比例 fit 进你指定的画布（保持长宽比、居中、留 8% 缓冲）。
- **AI 预处理（可选）**——渲染**前**用第三方图像模型编辑源图，经 OpenAI 兼容网关：
  - `gpt-image-2`（`/images/edits`）
  - `gemini-3.1-flash-image`（`/chat/completions`）
  - `grok-imagine-image-lite`（`/chat/completions`）
  - AI 处理**当前选中**的图，每次结果都缓存为新候选，可反复迭代。
- **候选历史**——左侧候选条保存原图 + 每次 AI 结果；点任一缩略图即设为要渲染的**目标图**。
- **导入 JSON**——加载已保存的 `*_engine.json` 形状文档，直接在预览区渲染（不重跑生成）。
- **渲染模式**——默认 / 贴纸（保留透明度）。
- **计算后端**——GPU（跨厂商 **OpenCL**）/ CPU / 自动，GPU 不可用时优雅降级到 CPU。

### 架构

```
┌────────────────────────────────────────────────────────────┐
│ 前端 (index.html / src/main.ts)：参数 · 候选条 · 目标图 ·       │
│   AI 弹窗 · 实时预览 · 分隔条                                   │
└───────────────┬──────────────────────────────────────────────┘
   invoke        │   ▲ listen("engine-event")
                 ▼   │
┌────────────────────────────────────────────────────────────┐
│ Rust 后端 (src-tauri/src/lib.rs)：命令                         │
│   start_generation · ai_process_image · read_image_data_url   │
│   · import_json —— spawn sidecar，line-JSON ↔ 事件桥接         │
└───────────────┬──────────────────────────────────────────────┘
   argv          │   ▲ stdout 逐行 JSON (meta/progress/frame/done/…)
                 ▼   │
┌────────────────────────────────────────────────────────────┐
│ Python sidecar (python/)：                                    │
│   sidecar.py      → fd6.shapegen.Engine.run()（含 preprocess）│
│   image_process.py→ 经网关做 AI 图像编辑                       │
│   render_json.py  → 渲染既有形状 JSON                          │
│ 引擎 vendoring 自 ForzaDesigner6（fd6/shapegen + fd6/io）      │
└────────────────────────────────────────────────────────────┘
```

### 运行（开发模式）

前提：**Rust**、**Node 18+**、**Python 3.10+**。

```powershell
# 1) Python 引擎依赖（一次性）；requests 供 AI 预处理 sidecar 调用网关
cd python
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install numpy pillow requests
cd ..

# 2) 前端依赖（一次性）
npm install

# 3) 启动（同时起 vite 前端 + Rust 后端 + 应用窗口）
npm run tauri dev
```

只预览纯前端（不带引擎/AI）：`npm run dev` 后打开 `http://localhost:1420`。

### 用法

1. 点左侧候选条里的虚线 **`+`** 占位选一张本地图片（成为候选①「原图」）。
2. *（可选）* **AI**：在输入框写处理指令，点模型名展开弹窗设置 **API Key** 并选模型，按 **Enter**（或 ↵ 按钮）。结果缓存为新候选并自动选中。
3. 点候选条任一缩略图，选定要渲染的**目标图**（原图或某个 AI 结果）。
4. 设 **画布宽 × 高**、**目标形状数**、**渲染模式**、**计算后端**，点 **开始渲染**。右侧实时填充；完成后在源图旁保存 `*_engine.json`（FD6 形状文档）。
5. 顶部 **导入 JSON**：加载既有 `*_engine.json`，在预览区渲染而不重跑生成。

### sidecar 协议（stdout 每行一个 JSON）

| type | 字段 | 含义 |
|---|---|---|
| `meta` | width, height | 处理后画布尺寸 |
| `backend` | message | 解析出的计算后端 |
| `progress` | shape_count, total, rms | 轻量进度 |
| `frame` | + png(base64) | 预览帧 |
| `done` | + png, json_path | 完成 + 已保存 JSON 路径 |
| `error` | message | 失败 |

### 技术栈

Tauri 2（Rust 外壳）· Vite + TypeScript（原生前端）· Python sidecar（numpy + pillow + requests）· 引擎 vendoring 自 **ForzaDesigner6**。

### 已知限制

- **仅开发模式**——sidecar 路径用 `CARGO_MANIFEST_DIR` 定位；打包（PyInstaller + Tauri 资源）待办。
- **AI Key 处理**——Key 运行时输入、经 argv 传给 sidecar，**不落盘、不进日志**，但运行瞬间对本地进程列表可见；改走 stdin 是计划中的加固。
- `grok-imagine-image-lite` 可能因网关上游不可用返回 `503（无可用渠道）`。

---

## License / 许可

The vendored engine originates from [ForzaDesigner6](https://github.com/tokyubevoxelverse/ForzaDesigner6); please refer to and respect the upstream project's license.
vendoring 的引擎源自 [ForzaDesigner6](https://github.com/tokyubevoxelverse/ForzaDesigner6)，请参考并遵循上游项目的许可协议。
