#!/usr/bin/env pwsh
# Forza Engine Studio 发布构建钩子（本地安装包，不接后端）：
#   冻结 Python sidecar (PyInstaller) → 同步版本号 → npm run tauri build
#   → 产出 Windows 安装包 (MSI / NSIS)。
#
# 用法（在项目根目录）：
#   .\build-release.ps1                      # 用当前版本号构建
#   .\build-release.ps1 -Version 0.2.0       # 先把版本号写入三处清单再构建
#   .\build-release.ps1 -SkipSidecar         # 复用已构建的 sidecar（仅重打前端/壳）
#   .\build-release.ps1 -DebugBuild          # tauri build --debug（更快，含调试符号）
#
# 前置：python venv（见 README 开发环境）、Node、Rust。PyInstaller 缺失时本脚本自动安装到 venv。

[CmdletBinding()]
param(
    [string]$Version,
    [switch]$SkipSidecar,
    [switch]$DebugBuild
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

function Need-Tool($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "缺少必要工具：$name —— 请安装并加入 PATH 后重试。"
    }
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    # Windows PowerShell 5.1 的 `Set-Content -Encoding utf8` 会写 BOM——Cargo 拒绝带 BOM 的
    # Cargo.toml，serde-json 也不吃 BOM。用 .NET 直接写无 BOM UTF-8，跨 PS 版本一致。
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

# ---------------------------------------------------------------------------
# 1. 工具检查
# ---------------------------------------------------------------------------
Need-Tool npm
Need-Tool cargo
Need-Tool rustc

$tauriConf = Join-Path $root 'src-tauri\tauri.conf.json'
$cargoToml = Join-Path $root 'src-tauri\Cargo.toml'
$pkgJson   = Join-Path $root 'package.json'

# ---------------------------------------------------------------------------
# 2. 可选：升级版本号（tauri.conf.json / Cargo.toml / package.json 三处同步）
#    用定点正则替换以保留各文件原有格式；版本号为唯一可信来源。
# ---------------------------------------------------------------------------
if ($Version) {
    if ($Version -notmatch '^\d+\.\d+\.\d+$') {
        throw "非法版本号（需 MAJOR.MINOR.PATCH）：$Version"
    }
    Write-Utf8NoBom $tauriConf ((Get-Content -Raw $tauriConf) -replace '("version"\s*:\s*")[^"]*(")', "`${1}$Version`${2}")
    # 仅替换 [package] 段内的 version，避免命中将来可能出现的 [dependencies.x] 表式 version 行。
    $cargoText = [regex]::Replace((Get-Content -Raw $cargoToml),
        '(?ms)(^\[package\]\s.*?^version\s*=\s*")[^"]*(")', "`${1}$Version`${2}")
    Write-Utf8NoBom $cargoToml $cargoText
    Write-Utf8NoBom $pkgJson ((Get-Content -Raw $pkgJson) -replace '("version"\s*:\s*")[^"]*(")', "`${1}$Version`${2}")
    Write-Host "[version] 已写入 $Version（tauri.conf.json / Cargo.toml / package.json）" -ForegroundColor Cyan
}

$effVersion = [regex]::Match((Get-Content -Raw $tauriConf), '"version"\s*:\s*"([^"]+)"').Groups[1].Value
if (-not $effVersion) { throw "无法从 tauri.conf.json 解析版本号。" }
Write-Host "[build] 版本号: $effVersion" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# 3. 冻结 Python sidecar（PyInstaller onedir → src-tauri\pyengine\fes-engine\）
#    产物由 Tauri 以 bundle.resources 打进安装包；Rust 端在打包态优先调用它。
# ---------------------------------------------------------------------------
$pyDir       = Join-Path $root 'python'
$venvPy      = Join-Path $pyDir '.venv\Scripts\python.exe'
$engineDir   = Join-Path $root 'src-tauri\pyengine\fes-engine'
$engineExe   = Join-Path $engineDir 'fes-engine.exe'
$internalDir = Join-Path $engineDir '_internal'
$gitkeep     = Join-Path $engineDir '.gitkeep'
$gitkeepBody = '# Keep the Tauri resource dir present at build time. PyInstaller fills this dir; real contents are git-ignored. See RELEASE.md.'

if ($SkipSidecar) {
    if (-not (Test-Path $engineExe) -or -not (Test-Path $internalDir)) {
        throw "-SkipSidecar 但未找到完整 sidecar onedir（缺 fes-engine.exe 或 _internal\）：$engineDir（先去掉该开关构建一次）。"
    }
    & $engineExe --help *> $null
    if ($LASTEXITCODE -ne 0) { throw "-SkipSidecar 复用的产物不可运行 (exit=$LASTEXITCODE)：$engineExe" }
    Write-Host "[sidecar] 复用已构建产物：$engineDir" -ForegroundColor DarkGray
} else {
    if (-not (Test-Path $venvPy)) {
        throw "未找到 Python venv：$venvPy —— 请按 README 创建并安装 numpy/pillow/requests。"
    }
    & $venvPy -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[sidecar] venv 内安装 PyInstaller…" -ForegroundColor Cyan
        & $venvPy -m pip install pyinstaller
        if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller 失败 (exit=$LASTEXITCODE)" }
    }
    Write-Host "[sidecar] PyInstaller 冻结 → $engineDir" -ForegroundColor Cyan
    & $venvPy -m PyInstaller (Join-Path $pyDir 'fes-engine.spec') `
        --noconfirm `
        --distpath (Join-Path $root 'src-tauri\pyengine') `
        --workpath (Join-Path $root 'build\pyi')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败 (exit=$LASTEXITCODE)" }
    if (-not (Test-Path $engineExe)) { throw "未生成 $engineExe" }

    # 冒烟自检：冻结 exe 能启动并打印用法（验证 bootloader + 依赖装载没炸）。
    & $engineExe --help *> $null
    if ($LASTEXITCODE -ne 0) { throw "冻结 sidecar 冒烟自检失败 (exit=$LASTEXITCODE)：$engineExe" }
    # PyInstaller 的 COLLECT 会清空并重建该目录，删掉占位 .gitkeep；补回来，避免 git 误报删除。
    Write-Utf8NoBom $gitkeep $gitkeepBody
    Write-Host "[sidecar] OK：$engineExe" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 4. 前端依赖（首次）
# ---------------------------------------------------------------------------
if (-not (Test-Path (Join-Path $root 'node_modules'))) {
    Write-Host "[frontend] 安装前端依赖 (npm install)…" -ForegroundColor Cyan
    & npm install
    if ($LASTEXITCODE -ne 0) { throw "npm install 失败 (exit=$LASTEXITCODE)" }
}

# ---------------------------------------------------------------------------
# 5. tauri build：beforeBuildCommand 跑 tsc + vite build，随后 cargo build --release
#    + 打包（MSI / NSIS）。sidecar 资源已就位，bundler 会一并打入。
# ---------------------------------------------------------------------------
$tauriArgs = @('run', 'tauri', 'build')
if ($DebugBuild) { $tauriArgs += '--debug' }
Push-Location $root
try {
    Write-Host "[build] npm $($tauriArgs -join ' ')" -ForegroundColor Cyan
    & npm @tauriArgs
    if ($LASTEXITCODE -ne 0) { throw "tauri build 失败 (exit=$LASTEXITCODE)" }
} finally {
    Pop-Location
}

# ---------------------------------------------------------------------------
# 6. 报告产物
# ---------------------------------------------------------------------------
$buildProfile = if ($DebugBuild) { 'debug' } else { 'release' }
$bundleDir = Join-Path $root "src-tauri\target\$buildProfile\bundle"
Write-Host "`n[done] 版本 $effVersion 安装包产物：" -ForegroundColor Green
$artifacts = Get-ChildItem -Recurse -File -Path $bundleDir -Include *.msi, *.exe -ErrorAction SilentlyContinue
if ($artifacts) {
    $artifacts | ForEach-Object { Write-Host "  $($_.FullName)" -ForegroundColor Green }
} else {
    Write-Warning "未在 $bundleDir 找到 .msi / .exe 安装包，请检查 tauri build 输出。"
}
