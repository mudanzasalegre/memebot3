param(
    [switch]$KeepArtifacts
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$StartStack = Join-Path $RepoRoot "scripts\start_stack.ps1"

if (-not (Test-Path $StartStack)) {
    throw "start_stack.ps1 not found: $StartStack"
}

Set-Location $RepoRoot

& powershell.exe -ExecutionPolicy Bypass -File $StartStack `
    -IncludeBot `
    -SkipBot `
    -SkipApi `
    -SkipUi `
    -AutoResearchOnce `
    -SkipStartupPreflight `
    -AutoResearchSpace moonshot_micro `
    -AutoResearchMaxCandidates 1 `
    -AutoResearchMaxParallel 1 `
    -AutoResearchMode grid `
    -AutoResearchSeed 1013

if ($LASTEXITCODE -ne 0) {
    throw "start_stack AutoResearch smoke failed with exit_code=$LASTEXITCODE"
}

$RequiredFiles = @(
    "data\research_runs\autoresearch_runtime_state.json",
    "data\research_runs\autoresearch_events.jsonl",
    "data\metrics\acquisition_health_report.json",
    "data\metrics\current_run_autotune_state.json"
)

foreach ($RelativePath in $RequiredFiles) {
    $Path = Join-Path $RepoRoot $RelativePath
    if (-not (Test-Path $Path)) {
        throw "Required start_stack artifact missing: $RelativePath"
    }
}

$StatePath = Join-Path $RepoRoot "data\research_runs\autoresearch_runtime_state.json"
$State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
if ($State.live_promotion_enabled -ne $false) {
    throw "AutoResearch live_promotion_enabled must remain false"
}
if ($State.auto_live_promote -ne $false) {
    throw "AutoResearch auto_live_promote must remain false"
}

$EventsPath = Join-Path $RepoRoot "data\research_runs\autoresearch_events.jsonl"
$Events = Get-Content -LiteralPath $EventsPath | ForEach-Object { $_ | ConvertFrom-Json }
$EventNames = @($Events | ForEach-Object { $_.event })
foreach ($RequiredEvent in @("AUTORESEARCH_START", "AUTORESEARCH_CYCLE_START", "AUTORESEARCH_CYCLE_END", "AUTORESEARCH_STOP")) {
    if ($EventNames -notcontains $RequiredEvent) {
        throw "Required AutoResearch event missing: $RequiredEvent"
    }
}

Write-Host "test_start_stack_autoresearch=ok"
