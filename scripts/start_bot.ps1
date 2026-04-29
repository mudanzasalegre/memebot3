param(
    [switch]$RealMode,
    [switch]$NoFileLog
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$BotScript = Join-Path $RepoRoot "run_bot.py"

if (-not (Test-Path $Python)) {
    throw "Project venv not found at $Python"
}
if (-not (Test-Path $BotScript)) {
    throw "run_bot.py not found at $BotScript"
}

Set-Location $RepoRoot

$Args = @($BotScript)
if (-not $RealMode) {
    $Args += "--dry-run"
}
if (-not $NoFileLog) {
    $Args += "--log"
}

Write-Host "[start_bot] repo=$RepoRoot"
Write-Host ("[start_bot] mode={0}" -f ($(if ($RealMode) { "real" } else { "dry-run" })))
Write-Host ("[start_bot] file_log={0}" -f $(if ($NoFileLog) { "off" } else { "on" }))
& $Python @Args
