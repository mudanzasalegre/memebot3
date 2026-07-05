param(
    [switch]$Once,
    [switch]$RegenerateReports,
    [switch]$SkipRegenerateReports,
    [switch]$NoPaperPromote,
    [switch]$NoDemotion,
    [string]$Space = "",
    [int]$MaxCandidates = 3,
    [int]$MaxParallel = 1,
    [string]$Mode = "seeded_random",
    [double]$IntervalHours = 1.0,
    [int]$Seed = -1,
    [int]$RestartDelaySeconds = 30,
    [int]$MaxRestarts = 0
)

$ErrorActionPreference = "Stop"

if ($RegenerateReports -and $SkipRegenerateReports) {
    throw "Use either -RegenerateReports or -SkipRegenerateReports, not both."
}
if ($RestartDelaySeconds -lt 1) {
    throw "-RestartDelaySeconds must be >= 1."
}
if ($MaxRestarts -lt 0) {
    throw "-MaxRestarts must be >= 0. Use 0 for unlimited restarts."
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$AutoResearchTool = Join-Path $RepoRoot "tools\run_autoresearch_loop.py"
$DefaultConfigProfile = "paper_hotfix_runner_v2"
$StackConfigProfile = if ([string]::IsNullOrWhiteSpace($env:CONFIG_PROFILE)) { $DefaultConfigProfile } else { $env:CONFIG_PROFILE.Trim() }

if (-not (Test-Path $Python)) {
    throw "Project venv not found at $Python"
}
if (-not (Test-Path $AutoResearchTool)) {
    throw "AutoResearch loop tool not found at $AutoResearchTool"
}

Set-Location $RepoRoot

$ShouldRegenerateReports = $RegenerateReports -and (-not $SkipRegenerateReports)
$LogDir = Join-Path $RepoRoot "data\research_runs\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogPath = Join-Path $LogDir "autoresearch_daemon_$Stamp.log"
$env:AUTORESEARCH_LOG_PATH = $LogPath

# Force the AutoResearch process into the paper/replay-only contract. These
# process-local values do not edit .env and cannot enable live promotion.
$env:CONFIG_PROFILE = $StackConfigProfile
$env:AUTORESEARCH_ENABLED = "true"
$env:AUTORESEARCH_MODE = "paper_replay"
$env:AUTORESEARCH_API_BUDGET_AWARE = "true"
$env:AUTORESEARCH_LIVE_PROMOTION_ENABLED = "false"
$env:AUTORESEARCH_AUTO_LIVE_PROMOTE = "false"
$env:AUTORESEARCH_LLM_ENABLED = "false"
$env:AUTORESEARCH_LLM_CAN_EDIT_CODE = "false"
$env:AUTORESEARCH_LLM_CAN_TOUCH_LIVE = "false"
$env:AUTORESEARCH_LLM_CAN_CALL_APIS = "false"
$env:AUTO_PROMOTE_LIVE = "false"
$env:MODEL_AUTO_PROMOTE = "false"
$env:ML_AUTO_PROMOTE_LANES = "false"

$Args = @($AutoResearchTool)
if ($Once) {
    $Args += "--once"
} else {
    $Args += "--daemon"
}

if ($Space.Trim()) {
    $Args += @("--space", $Space)
}
if ($Seed -ge 0) {
    $Args += @("--seed", "$Seed")
}

$Args += @(
    "--max-candidates", "$MaxCandidates",
    "--max-parallel", "$MaxParallel",
    "--mode", $Mode,
    "--interval-hours", "$IntervalHours"
)

if ($ShouldRegenerateReports) {
    $Args += "--regenerate-reports"
}
if ($NoPaperPromote) {
    $Args += "--no-paper-promote"
}
if ($NoDemotion) {
    $Args += "--no-demotion"
}

Write-Host "[start_autoresearch] repo=$RepoRoot"
Write-Host ("[start_autoresearch] mode={0}" -f $(if ($Once) { "once" } else { "daemon" }))
Write-Host ("[start_autoresearch] space={0}" -f $(if ($Space.Trim()) { $Space } else { "bandit/idle" }))
Write-Host "[start_autoresearch] max_candidates=$MaxCandidates max_parallel=$MaxParallel interval_hours=$IntervalHours"
Write-Host "[start_autoresearch] regenerate_reports=$ShouldRegenerateReports"
Write-Host "[start_autoresearch] live_promotion=false llm_touch_live=false"
Write-Host "AutoResearch process starting"
Write-Host "CONFIG_PROFILE=$StackConfigProfile"
Write-Host "AUTORESEARCH_ENABLED=true"
Write-Host "AUTORESEARCH_MODE=paper_replay"
Write-Host "AUTORESEARCH_LIVE_PROMOTION_ENABLED=false"
Write-Host "AUTORESEARCH_AUTO_LIVE_PROMOTE=false"
Write-Host "AUTORESEARCH_LOG_PATH=$LogPath"
Write-Host "AutoResearch command: .\.venv\Scripts\python.exe tools\run_autoresearch_loop.py"

$RestartCount = 0
while ($true) {
    Write-Host "[start_autoresearch] launch=$RestartCount"
    & $Python @Args 2>&1 | Tee-Object -FilePath $LogPath -Append
    $ExitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    Write-Host "[start_autoresearch] exit_code=$ExitCode log=$LogPath"

    if ($Once) {
        exit $ExitCode
    }

    $RestartCount += 1
    if ($MaxRestarts -gt 0 -and $RestartCount -gt $MaxRestarts) {
        exit $ExitCode
    }

    Write-Warning "[start_autoresearch] daemon exited unexpectedly; restarting in $RestartDelaySeconds seconds"
    Start-Sleep -Seconds $RestartDelaySeconds
}
