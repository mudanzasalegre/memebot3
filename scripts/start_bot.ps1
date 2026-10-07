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
$DefaultConfigProfile = "paper_hotfix_0707"

$SelectedConfigProfile = if ([string]::IsNullOrWhiteSpace($env:CONFIG_PROFILE)) { $DefaultConfigProfile } else { $env:CONFIG_PROFILE.Trim() }
$InheritedConfigProfilePath = [string]$env:CONFIG_PROFILE_PATH
if (-not [string]::IsNullOrWhiteSpace($InheritedConfigProfilePath)) {
    Write-Warning "[start_bot] clearing inherited CONFIG_PROFILE_PATH because the launcher selected named profile '$SelectedConfigProfile'"
}
[Environment]::SetEnvironmentVariable("CONFIG_PROFILE_PATH", $null, "Process")
$env:CONFIG_PROFILE = $SelectedConfigProfile

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

$RequestedMode = if ($RealMode) { "real" } else { "dry-run" }
$EffectiveMode = "dry-run"
if ($RealMode) {
    $ConfigModeOutput = @(& $Python -c "from config.config import CFG; print('dry-run' if bool(CFG.DRY_RUN) else 'real')")
    if ($LASTEXITCODE -ne 0) {
        throw "Could not resolve the effective bot mode from config (exit_code=$LASTEXITCODE)"
    }
    $ResolvedModes = @($ConfigModeOutput | ForEach-Object { [string]$_ } | Where-Object { $_ -in @("dry-run", "real") })
    if ($ResolvedModes.Count -ne 1) {
        throw "Could not resolve a unique effective bot mode from config"
    }
    $EffectiveMode = $ResolvedModes[0]
    if ($EffectiveMode -ne "real") {
        Write-Warning "[start_bot] -RealMode was requested, but the selected profile enforces dry-run"
    }
}

Write-Host "[start_bot] repo=$RepoRoot"
Write-Host "[start_bot] config_profile=$SelectedConfigProfile"
Write-Host "[start_bot] requested_mode=$RequestedMode"
Write-Host "[start_bot] effective_mode=$EffectiveMode"
Write-Host ("[start_bot] file_log={0}" -f $(if ($NoFileLog) { "off" } else { "on" }))
& $Python @Args
