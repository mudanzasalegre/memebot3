param(
    [switch]$Once,
    [switch]$RegenerateReports,
    [switch]$NoPaperPromote,
    [switch]$NoDemotion,
    [string]$Space = "",
    [int]$MaxCandidates = 3,
    [int]$MaxParallel = 1,
    [string]$Mode = "seeded_random",
    [double]$IntervalHours = 6.0,
    [int]$Seed = -1
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$AutoResearchTool = Join-Path $RepoRoot "tools\run_autoresearch_loop.py"

if (-not (Test-Path $Python)) {
    throw "Project venv not found at $Python"
}
if (-not (Test-Path $AutoResearchTool)) {
    throw "AutoResearch loop tool not found at $AutoResearchTool"
}

Set-Location $RepoRoot

# Force the AutoResearch process into the paper/replay-only contract. These
# process-local values do not edit .env and cannot enable live promotion.
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

if ($RegenerateReports) {
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
Write-Host "[start_autoresearch] live_promotion=false llm_touch_live=false"
& $Python @Args
