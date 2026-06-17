# 发布手册 / Release Guide — Forza Engine Studio

本手册说明如何把 Forza Engine Studio 打成可分发的 **Windows 安装包**。一句话：

```powershell
.\build-release.ps1 -Version 0.2.0
```

会自动完成「冻结 Python 引擎 → 同步版本号 → 构建前端 → 编译 Rust 壳 → 打 MSI/NSIS 安装包」，产物落在 `src-tauri\target\release\bundle\`。

> 仅支持 **Windows**。分发模型是**本地安装包**——直接把安装包发给用户安装，不接任何后端、不做自动更新、不上传任何东西。

---

## 1. 前置环境

| 依赖 | 说明 |
| --- | --- |
| **Rust** (stable, 1.77+) | 编译 Tauri 壳。`rustup` 安装。 |
| **Node 18+** | 构建前端（Vite + React + Semi）。 |
| **Python 3.10+** + venv | 冻结引擎 sidecar。需在 `python\.venv` 内装好 `numpy pillow requests`（见 README「开发环境」）。 |
| **WebView2** | Windows 11 自带；Win10 若缺失由安装包引导安装。 |
| **MSVC Build Tools** | VS 2022 的 C++ 生成工具（Rust MSVC 工具链需要）。 |

PyInstaller 不必手动装——`build-release.ps1` 会在 `python\.venv` 内按需 `pip install pyinstaller`。

首次构建会拉取大量 Rust crate（数百 MB，数分钟），之后增量编译很快。

---

## 2. 版本号（唯一可信来源）

版本号同时存在于三处清单：`src-tauri\tauri.conf.json`、`src-tauri\Cargo.toml`、`package.json`。**不要手改**——用脚本的 `-Version` 一次性同步：

```powershell
.\build-release.ps1 -Version 0.2.0   # 写入三处并构建
```

安装包文件名、安装目录、控制面板里的版本号都来自 `tauri.conf.json` 的 `version`，由该步统一写入。不带 `-Version` 时沿用当前 `tauri.conf.json` 里的版本。

版本号必须是 `MAJOR.MINOR.PATCH`（如 `0.2.0`），否则脚本拒绝执行。

---

## 3. 打包是怎么工作的（Python sidecar → 安装包）

开发态下，Rust 后端用 `python\.venv` 直接跑 `python sidecar.py …`（路径靠 `CARGO_MANIFEST_DIR` 定位）。这套**不能分发**——用户机器上没有 Python，也没有 venv。

发布态用 **PyInstaller** 把引擎冻结成一个自带解释器的可执行文件，再由 Tauri 打进安装包：

```
python\fes_engine.py            ← 调度入口：一个 exe，三个子命令
  ├─ generate     → sidecar.py        （渲染）
  ├─ ai           → image_process.py  （AI 预处理）
  └─ render-json  → render_json.py    （导入 JSON 渲染）
        │  PyInstaller (python\fes-engine.spec, onedir)
        ▼
src-tauri\pyengine\fes-engine\fes-engine.exe + _internal\…   ← 冻结产物（git 忽略）
        │  Tauri bundle.resources: ["pyengine/fes-engine/"]
        ▼
<安装目录>\resources\pyengine\fes-engine\fes-engine.exe       ← 随安装包分发
```

Rust 后端（`src-tauri\src\lib.rs` 的 `engine_command`）在运行时自动选择：

- **打包态**：`resource_dir()\pyengine\fes-engine\fes-engine.exe` 存在 → 调用冻结 exe，并把子命令（`generate`/`ai`/`render-json`）作为首个参数。
- **开发态**（`tauri dev` / `cargo run`）：资源不存在 → 回退到 `python\.venv` + 松散脚本。

判定依据是**冻结 exe 是否存在**，所以 `tauri dev` 完全不受影响，照常用 venv 调试。

> `src-tauri\pyengine\fes-engine\.gitkeep` 是占位文件：Tauri 在 `cargo build` 阶段会校验资源源路径必须存在（含 `tauri dev`），故提交一个占位文件让目录始终在；真正的 `fes-engine.exe` + `_internal\` 由 PyInstaller 生成，已被 git 忽略。

### GPU（OpenCL）说明

按 `python\fd6\shapegen\gpu.py` 的精简设计，**pyopencl 不打进安装包**：用户首次显式选 GPU 时，由冻结 exe 内置的 `pip` 在线安装匹配 cp312 ABI 的 pyopencl 到 `%LOCALAPPDATA%\FD6\gpu_runtime`（OpenCL 运行时本身随显卡驱动提供）。因此：

- 冻结 exe 里**打包了 `pip`**，让上述按需安装在脱离 Python 环境时也能工作；
- 无 GPU / 无网络时，引擎静默回退到打包内置的 **CPU 路径**，渲染照常。

---

## 4. 一键发布

```powershell
# 标准发布（升版本号 → 全量构建 → 出安装包）
.\build-release.ps1 -Version 0.2.0

# 沿用当前版本号
.\build-release.ps1

# 仅重打前端/壳，复用上次冻结好的 sidecar（改了 React/Rust、没动 Python 时更快）
.\build-release.ps1 -SkipSidecar

# 调试版安装包（更快，含符号；产物在 target\debug\bundle）
.\build-release.ps1 -DebugBuild
```

脚本依次执行：

1. 检查 `npm` / `cargo` / `rustc` 是否在 PATH。
2.（带 `-Version` 时）把版本号定点写入三处清单。
3. 用 `python\.venv` 跑 PyInstaller 冻结 sidecar 到 `src-tauri\pyengine\fes-engine\`，并对冻结 exe 做一次启动冒烟自检。
4.（首次）`npm install`。
5. `npm run tauri build`：`beforeBuildCommand` 跑 `tsc && vite build`，随后 `cargo build --release` + 打包。
6. 列出 `src-tauri\target\release\bundle\` 下的 `.msi` / `.exe` 安装包路径。

---

## 5. 产物在哪

```
src-tauri\target\release\bundle\
  └─ nsis\  forzaenginestudio_0.2.0_x64-setup.exe     ← NSIS 安装程序
```

`tauri.conf.json` 里 `bundle.targets: ["nsis"]` 产出 NSIS 安装程序——它对 PyInstaller 那种「一个 exe + 上百个 `_internal\` 文件」的大资源树最稳。
> 不用 MSI 的原因：WiX 的 `light.exe` 在链接这么大的 `_internal\` 文件树时会失败。如确需 MSI，把 targets 改成 `["nsis", "msi"]` 并自行排查 WiX 工具链（ICE 校验 / 文件数上限等）。

---

## 6. 发布前检查清单

```powershell
# 1) 回到 main 并拉最新
git switch main
git pull --ff-only origin main

# 2) 后端能编过
Push-Location .\src-tauri; cargo check; Pop-Location

# 3) 引擎单测（如已配置 venv）
.\python\.venv\Scripts\python.exe -m pytest python\tests

# 4) 出包
.\build-release.ps1 -Version 0.2.0

# 5) 在一台「干净」机器（无 Python / 无 venv）装上安装包，验证：
#    渲染（CPU 至少要通）、AI 预处理、导入 JSON —— 都不依赖开发环境即可工作。
```

---

## 7. 注意事项 / 排错

- **未签名的告警**：PyInstaller 产出的 `fes-engine.exe` 与未签名的安装包可能触发 Windows SmartScreen / 杀软误报。如需正式分发，建议对安装包（及内嵌的 `fes-engine.exe`）做代码签名。
- **冻结 exe 起不来 / 渲染没反应**：多半是 PyInstaller 漏收依赖。先单独验证冻结产物：
  ```powershell
  src-tauri\pyengine\fes-engine\fes-engine.exe render-json --json <某个_engine.json>
  ```
  能打出一行 `{"type":"done",...}` 就说明 numpy/Pillow/多进程都正常。缺包就在 `python\fes-engine.spec` 的 `hiddenimports` 里补。
- **CPU 渲染用了多进程**：引擎用 `ProcessPoolExecutor`（Windows 走 spawn），靠 `fes_engine.py` 里第一行的 `multiprocessing.freeze_support()` 保证冻结后子进程正常 bootstrap——别移动它的位置。
- **改了 Python 引擎后**：务必重跑一次不带 `-SkipSidecar` 的构建，否则安装包里还是旧的冻结 exe。
- **`-SkipSidecar` 报找不到产物**：说明还没冻结过，先去掉该开关完整构建一次。

---

## 8. 与参考项目（ds_v9）的差异

本流程参考了 ds_v9 的发版脚本与手册，但因分发模型不同做了裁剪：

| 维度 | ds_v9（游戏 trainer） | 本项目（离线创作工具） |
| --- | --- | --- |
| 产物 | 裸 `exe`（`bundle.active:false`） | NSIS 安装包（`bundle.targets:["nsis"]`） |
| 分发 | 上传中台后端 + 客户端自动更新 | 本地安装包，手动分发 |
| 签名 | 每次构建生成唯一签名供后端校验 | 无（无后端校验，故不需要） |
| 加固 | obfstr + 防逆向字符串扫描 | 无（非对抗场景） |
| Python | 无 | PyInstaller 冻结 sidecar 打进安装包 |
