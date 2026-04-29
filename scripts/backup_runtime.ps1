$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Script = Join-Path $RepoRoot "scripts\runtime_backup.py"

if (-not (Test-Path $Python)) {
    throw "Project venv not found at $Python"
}

Set-Location $RepoRoot
& $Python $Script @args
