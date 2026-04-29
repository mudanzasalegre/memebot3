param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$Reload,
    [switch]$OpenDocs
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "Project venv not found at $Python"
}

Set-Location $RepoRoot

if ($OpenDocs) {
    Start-Process "http://$BindHost`:$Port/docs" | Out-Null
}

$Args = @(
    "-m",
    "uvicorn",
    "api.main:app",
    "--host", $BindHost,
    "--port", "$Port"
)

if ($Reload) {
    $Args += "--reload"
}

Write-Host "[start_api] repo=$RepoRoot"
Write-Host "[start_api] url=http://$BindHost`:$Port"
& $Python @Args
