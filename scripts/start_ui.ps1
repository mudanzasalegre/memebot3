param(
    [string]$ApiProxyTarget = "http://127.0.0.1:8000",
    [switch]$InstallIfMissing
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$UiRoot = Join-Path $RepoRoot "ui"
$NodeModules = Join-Path $UiRoot "node_modules"

if (-not (Test-Path $UiRoot)) {
    throw "UI directory not found at $UiRoot"
}

Set-Location $UiRoot

if ($InstallIfMissing -or -not (Test-Path $NodeModules)) {
    Write-Host "[start_ui] installing npm dependencies"
    npm install
}

$env:VITE_API_PROXY_TARGET = $ApiProxyTarget

Write-Host "[start_ui] repo=$UiRoot"
Write-Host "[start_ui] proxy=$ApiProxyTarget"
Write-Host "[start_ui] url=http://127.0.0.1:5173"
npm run dev
