#!/usr/bin/env pwsh
# Forza Engine Studio 开发运行：起 Vite 前端 + Rust 后端 + 应用窗口（= npm run tauri dev）。
# 首次会自动补齐前端依赖。Python 引擎依赖（numpy/pillow/requests）请按 README 在 python\.venv 里装好。
#
# 用法（在项目根目录）：
#   .\run.ps1                 # 等价于 npm run tauri dev
#   .\run.ps1 --release       # 多余参数透传给 tauri（如 --release 跑优化后端）
$ErrorActionPreference = 'Stop'

Push-Location $PSScriptRoot
try {
    if (-not (Test-Path (Join-Path $PSScriptRoot 'node_modules'))) {
        Write-Host "[run] 安装前端依赖 (npm install)…" -ForegroundColor Cyan
        & npm install
        if ($LASTEXITCODE -ne 0) { throw "npm install 失败 (exit=$LASTEXITCODE)" }
    }
    if (-not (Test-Path (Join-Path $PSScriptRoot 'python\.venv\Scripts\python.exe'))) {
        Write-Warning "[run] 未发现 python\.venv —— 渲染 / AI / 导入 JSON 需要它。见 README 的开发环境搭建。"
    }
    Write-Host "[run] npm run tauri dev $args" -ForegroundColor Cyan
    & npm run tauri dev -- @args
} finally {
    Pop-Location
}
