param(
    [switch]$RealMode,
    [switch]$NoFileLog,
    [switch]$SkipPolicyPreflightStatus
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$BotScript = Join-Path $RepoRoot "run_bot.py"
$PreflightTool = Join-Path $RepoRoot "tools\preflight.py"
$PreflightStatus = Join-Path $RepoRoot "data\metrics\preflight_status.json"

if (-not (Test-Path $Python)) {
    throw "Project venv not found at $Python"
}
if (-not (Test-Path $BotScript)) {
    throw "run_bot.py not found at $BotScript"
}

Set-Location $RepoRoot

if (-not $SkipPolicyPreflightStatus -and (Test-Path $PreflightTool)) {
    $ShouldRefreshPreflight = -not (Test-Path $PreflightStatus)
    if (-not $ShouldRefreshPreflight) {
        try {
            $ExistingPreflight = Get-Content -LiteralPath $PreflightStatus -Raw | ConvertFrom-Json
            $ShouldRefreshPreflight = -not [bool]$ExistingPreflight.ok
        } catch {
            $ShouldRefreshPreflight = $true
        }
    }
    if ($ShouldRefreshPreflight) {
        Write-Host "[start_bot] preparing Policy Center preflight status"
        & $Python $PreflightTool
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "[start_bot] Policy Center preflight status is not passing yet; bot startup will continue in paper/live mode requested"
        }
    }
}

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
