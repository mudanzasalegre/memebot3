param(
    [int]$IntervalSeconds = 3600,
    [int]$RestartDelaySeconds = 30,
    [int]$MaxRestarts = 0
)

$ErrorActionPreference = "Stop"

if ($IntervalSeconds -lt 60) {
    throw "-IntervalSeconds must be >= 60."
}
if ($RestartDelaySeconds -lt 1) {
    throw "-RestartDelaySeconds must be >= 1."
}
if ($MaxRestarts -lt 0) {
    throw "-MaxRestarts must be >= 0. Use 0 for unlimited restarts."
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$DaemonTool = Join-Path $RepoRoot "scripts\run_training_daemon.py"
$DefaultConfigProfile = "paper_hotfix_0707"
$StackConfigProfile = if ([string]::IsNullOrWhiteSpace($env:CONFIG_PROFILE)) { $DefaultConfigProfile } else { $env:CONFIG_PROFILE.Trim() }

if (-not (Test-Path $Python)) {
    throw "Project venv not found at $Python"
}
if (-not (Test-Path $DaemonTool)) {
    throw "Training daemon tool not found at $DaemonTool"
}

Set-Location $RepoRoot

$LogDir = Join-Path $RepoRoot "data\metrics\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogPath = Join-Path $LogDir "training_daemon_$Stamp.log"

$env:CONFIG_PROFILE = $StackConfigProfile
$env:ML_TRAINING_DAEMON_ENABLED = "true"
$env:ML_TRAINING_DAEMON_INTERVAL_S = "$IntervalSeconds"

Write-Host "[start_training_daemon] repo=$RepoRoot"
Write-Host "[start_training_daemon] interval_seconds=$IntervalSeconds"
Write-Host "CONFIG_PROFILE=$StackConfigProfile"
Write-Host "ML_TRAINING_DAEMON_ENABLED=true"
Write-Host "ML_TRAINING_DAEMON_INTERVAL_S=$IntervalSeconds"
Write-Host "TRAINING_DAEMON_LOG_PATH=$LogPath"
Write-Host "Training daemon command: .\.venv\Scripts\python.exe scripts\run_training_daemon.py"

$RestartCount = 0
while ($true) {
    Write-Host "[start_training_daemon] launch=$RestartCount"
    & $Python $DaemonTool 2>&1 | Tee-Object -FilePath $LogPath -Append
    $ExitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    Write-Host "[start_training_daemon] exit_code=$ExitCode log=$LogPath"

    $RestartCount += 1
    if ($MaxRestarts -gt 0 -and $RestartCount -gt $MaxRestarts) {
        exit $ExitCode
    }

    Write-Warning "[start_training_daemon] daemon exited unexpectedly; restarting in $RestartDelaySeconds seconds"
    Start-Sleep -Seconds $RestartDelaySeconds
}
