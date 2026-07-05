param(
    [switch]$IncludeBot,
    [switch]$IncludeAutoResearch,
    [switch]$SkipBot,
    [switch]$SkipAutoResearch,
    [switch]$SkipApi,
    [switch]$SkipUi,
    [switch]$BotRealMode,
    [switch]$UiInstallIfMissing,
    [switch]$SkipStartupPreflight,
    [switch]$SkipStartupTests,
    [switch]$SkipStartupSmoke,
    [switch]$SkipTrainingDaemon,
    [switch]$VisibleWindows,
    [switch]$AutoResearchOnce,
    [switch]$AutoResearchRegenerateReports,
    [switch]$AutoResearchSkipRegenerateReports,
    [switch]$AutoResearchNoPaperPromote,
    [switch]$AutoResearchNoDemotion,
    [string]$AutoResearchSpace = "",
    [int]$AutoResearchMaxCandidates = 3,
    [int]$AutoResearchMaxParallel = 1,
    [string]$AutoResearchMode = "seeded_random",
    [int]$AutoResearchIntervalMinutes = 60,
    [double]$AutoResearchIntervalHours = 0.0,
    [int]$AutoResearchSeed = -1,
    [int]$TrainingDaemonIntervalSeconds = 3600,
    [string]$ApiHost = "127.0.0.1",
    [int]$ApiPort = 8000,
    [string]$UiApiProxyTarget = "http://127.0.0.1:8000",
    [int]$ApiReadyTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

if ($AutoResearchRegenerateReports -and $AutoResearchSkipRegenerateReports) {
    throw "Use either -AutoResearchRegenerateReports or -AutoResearchSkipRegenerateReports, not both."
}
if ($IncludeAutoResearch -and $SkipAutoResearch) {
    throw "Use either -IncludeAutoResearch or -SkipAutoResearch, not both."
}
if ($AutoResearchIntervalMinutes -lt 1) {
    throw "-AutoResearchIntervalMinutes must be >= 1."
}
if ($AutoResearchIntervalHours -lt 0) {
    throw "-AutoResearchIntervalHours must be >= 0."
}
if ($TrainingDaemonIntervalSeconds -lt 60) {
    throw "-TrainingDaemonIntervalSeconds must be >= 60."
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ScriptsRoot = Join-Path $RepoRoot "scripts"
$PowerShellExe = "powershell.exe"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$DefaultConfigProfile = "paper_hotfix_runner_v2"
$StackConfigProfile = if ([string]::IsNullOrWhiteSpace($env:CONFIG_PROFILE)) { $DefaultConfigProfile } else { $env:CONFIG_PROFILE.Trim() }
$env:CONFIG_PROFILE = $StackConfigProfile
$CoreReportsPrepared = $false

$ResolvedAutoResearchIntervalHours = if ($AutoResearchIntervalHours -gt 0) {
    $AutoResearchIntervalHours
} else {
    [double]$AutoResearchIntervalMinutes / 60.0
}

function Assert-ProjectPython {
    param(
        [string]$Root
    )

    $ResolvedPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path $ResolvedPython)) {
        throw "Project venv not found at $ResolvedPython"
    }
    return $ResolvedPython
}

function Invoke-ProjectPython {
    param(
        [string]$Root,
        [string[]]$PythonArgs,
        [string]$Label
    )

    $ResolvedPython = Assert-ProjectPython -Root $Root
    Write-Host "[start_stack] preflight: $Label"
    & $ResolvedPython @PythonArgs
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit_code=$LASTEXITCODE"
    }
}

function Start-RepoWindow {
    param(
        [string]$ScriptName,
        [string[]]$ScriptArgs,
        [bool]$Visible = $false
    )

    $ScriptPath = Join-Path $ScriptsRoot $ScriptName
    if (-not (Test-Path $ScriptPath)) {
        throw "Script not found: $ScriptPath"
    }

    $Args = @(
        "-NoExit",
        "-ExecutionPolicy", "Bypass",
        "-File", $ScriptPath
    ) + $ScriptArgs

    $StartArgs = @{
        FilePath = $PowerShellExe
        WorkingDirectory = $RepoRoot
        ArgumentList = $Args
        PassThru = $true
    }
    if (-not $Visible) {
        $StartArgs.WindowStyle = "Hidden"
    }

    $Process = Start-Process @StartArgs
    Write-Host "[start_stack] started $ScriptName pid=$($Process.Id)"
}

function Wait-ApiReady {
    param(
        [string]$ApiBaseUrl,
        [int]$TimeoutSeconds
    )

    $HealthUrl = "{0}/api/v1/health" -f $ApiBaseUrl.TrimEnd("/")
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)

    Write-Host "[start_stack] waiting for api health: $HealthUrl"

    while ((Get-Date) -lt $Deadline) {
        try {
            $Response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2
            if ($Response.StatusCode -ge 200 -and $Response.StatusCode -lt 300) {
                Write-Host "[start_stack] api ready"
                return $true
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }

    Write-Warning "[start_stack] api did not answer health checks after $TimeoutSeconds seconds"
    return $false
}

$StackIncludesBot = $IncludeBot -and -not $SkipBot
$StackIncludesAutoResearch = ($IncludeAutoResearch -or $IncludeBot) -and -not $SkipAutoResearch
$StackIncludesTrainingDaemon = $StackIncludesBot -and -not $SkipTrainingDaemon
$AutoResearchShouldPrepareReports = $StackIncludesAutoResearch -and -not $AutoResearchSkipRegenerateReports
$StartupPreflightEnabled = ($StackIncludesBot -or $StackIncludesAutoResearch) -and -not $SkipStartupPreflight

function Set-AutoResearchSafeEnv {
    param(
        [string]$ConfigProfile
    )

    $env:CONFIG_PROFILE = $ConfigProfile
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
}

function Write-AutoResearchStartupLog {
    param(
        [string]$ConfigProfile
    )

    Write-Host "AutoResearch process starting"
    Write-Host "CONFIG_PROFILE=$ConfigProfile"
    Write-Host "AUTORESEARCH_ENABLED=true"
    Write-Host "AUTORESEARCH_MODE=paper_replay"
    Write-Host "AUTORESEARCH_LIVE_PROMOTION_ENABLED=false"
    Write-Host "AUTORESEARCH_AUTO_LIVE_PROMOTE=false"
    Write-Host "AutoResearch command: .\.venv\Scripts\python.exe tools\run_autoresearch_loop.py"
}

function Set-TrainingDaemonEnv {
    param(
        [int]$IntervalSeconds
    )

    $env:ML_TRAINING_DAEMON_ENABLED = "true"
    $env:ML_TRAINING_DAEMON_INTERVAL_S = "$IntervalSeconds"
}

function Test-ProcessIdRunning {
    param(
        [int]$ProcessId
    )

    if ($ProcessId -le 0) {
        return $false
    }

    try {
        $Process = Get-Process -Id $ProcessId -ErrorAction Stop
        return $Process.Id -eq $ProcessId
    } catch {
        return $false
    }
}

function Clear-StaleBotLock {
    param(
        [string]$Root
    )

    $LockPath = Join-Path $Root "data\run_bot.lock"
    if (-not (Test-Path $LockPath)) {
        return
    }

    $OwnerPid = 0
    try {
        $Owner = Get-Content $LockPath -Raw | ConvertFrom-Json
        if ($null -ne $Owner.pid) {
            $OwnerPid = [int]$Owner.pid
        }
    } catch {
        $OwnerPid = 0
    }

    if ($OwnerPid -gt 0 -and (Test-ProcessIdRunning -ProcessId $OwnerPid)) {
        Write-Host "[start_stack] existing bot lock is owned by live pid=$OwnerPid"
        return
    }

    try {
        Remove-Item -LiteralPath $LockPath -Force
        Write-Host "[start_stack] removed stale bot lock: $LockPath"
    } catch {
        Write-Warning "[start_stack] could not remove stale bot lock: $($_.Exception.Message)"
    }
}

function Invoke-CoreReportRegeneration {
    param(
        [string]$Root
    )

    $Tool = Join-Path $Root "tools\regenerate_core_reports.py"
    $ResolvedPython = Assert-ProjectPython -Root $Root
    if (-not (Test-Path $Tool)) {
        throw "Core report regeneration tool not found at $Tool"
    }

    Write-Host "[start_stack] regenerating core reports before bot/autoresearch startup"
    & $ResolvedPython $Tool
    if ($LASTEXITCODE -ne 0) {
        throw "Core report regeneration failed with exit_code=$LASTEXITCODE"
    }
    $script:CoreReportsPrepared = $true
}

function Invoke-PolicyPreflightStatus {
    param(
        [string]$Root,
        [bool]$TestsAlreadyPassed
    )

    $Args = @("tools\preflight.py")
    if ($TestsAlreadyPassed) {
        $Args += "--external-pytest-passed"
    }
    Invoke-ProjectPython -Root $Root -PythonArgs $Args -Label "policy preflight status"
}

function Invoke-StartupPreflight {
    param(
        [string]$Root,
        [bool]$RunTests,
        [bool]$RunAutoResearchSmoke
    )

    Write-Host "[start_stack] startup preflight starting"
    if ($RunTests) {
        Invoke-ProjectPython -Root $Root -PythonArgs @("-m", "pytest", "-q") -Label "pytest"
    } else {
        Write-Host "[start_stack] preflight: pytest skipped"
    }

    Invoke-CoreReportRegeneration -Root $Root

    if ($RunAutoResearchSmoke) {
        $SmokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("memebot3_autoresearch_smoke_" + [System.Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $SmokeRoot | Out-Null
        try {
            Invoke-ProjectPython -Root $Root -PythonArgs @("tools\autoresearch_smoke.py", "--root", $SmokeRoot) -Label "autoresearch smoke"
        } finally {
            if (Test-Path $SmokeRoot) {
                Remove-Item -LiteralPath $SmokeRoot -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    } else {
        Write-Host "[start_stack] preflight: autoresearch smoke skipped"
    }

    Invoke-ProjectPython -Root $Root -PythonArgs @("scripts\strategy_quality_gate.py", "--warn-only") -Label "strategy quality gate"
    Invoke-PolicyPreflightStatus -Root $Root -TestsAlreadyPassed:$RunTests
    Write-Host "[start_stack] startup preflight completed"
}

if ($StackIncludesBot) {
    Clear-StaleBotLock -Root $RepoRoot
}

if ($StackIncludesAutoResearch) {
    Set-AutoResearchSafeEnv -ConfigProfile $StackConfigProfile
}

if ($StackIncludesTrainingDaemon) {
    Set-TrainingDaemonEnv -IntervalSeconds $TrainingDaemonIntervalSeconds
}

if ($StartupPreflightEnabled) {
    Invoke-StartupPreflight -Root $RepoRoot -RunTests:(-not $SkipStartupTests) -RunAutoResearchSmoke:($StackIncludesAutoResearch -and -not $SkipStartupSmoke)
}

if ($AutoResearchShouldPrepareReports -and -not $CoreReportsPrepared) {
    Invoke-CoreReportRegeneration -Root $RepoRoot
}

if (-not $SkipApi) {
    Start-RepoWindow -ScriptName "start_api.ps1" -ScriptArgs @("-BindHost", $ApiHost, "-Port", "$ApiPort") -Visible:$VisibleWindows
}

if (-not $SkipUi) {
    $null = Wait-ApiReady -ApiBaseUrl $UiApiProxyTarget -TimeoutSeconds $ApiReadyTimeoutSeconds
    $UiArgs = @("-ApiProxyTarget", $UiApiProxyTarget)
    if ($UiInstallIfMissing) {
        $UiArgs += "-InstallIfMissing"
    }
    Start-RepoWindow -ScriptName "start_ui.ps1" -ScriptArgs $UiArgs -Visible:$VisibleWindows
}

if ($StackIncludesBot) {
    $BotArgs = @()
    if ($BotRealMode) {
        $BotArgs += "-RealMode"
    }
    Start-RepoWindow -ScriptName "start_bot.ps1" -ScriptArgs $BotArgs -Visible:$VisibleWindows
}

if ($StackIncludesTrainingDaemon) {
    Start-RepoWindow -ScriptName "start_training_daemon.ps1" -ScriptArgs @("-IntervalSeconds", "$TrainingDaemonIntervalSeconds") -Visible:$VisibleWindows
}

if ($StackIncludesAutoResearch) {
    $AutoResearchArgs = @(
        "-MaxCandidates", "$AutoResearchMaxCandidates",
        "-MaxParallel", "$AutoResearchMaxParallel",
        "-Mode", $AutoResearchMode,
        "-IntervalHours", "$ResolvedAutoResearchIntervalHours"
    )
    if ($AutoResearchOnce) {
        $AutoResearchArgs += "-Once"
    }
    if ($AutoResearchRegenerateReports) {
        $AutoResearchArgs += "-RegenerateReports"
    }
    if ($AutoResearchSkipRegenerateReports) {
        $AutoResearchArgs += "-SkipRegenerateReports"
    }
    if ($AutoResearchNoPaperPromote) {
        $AutoResearchArgs += "-NoPaperPromote"
    }
    if ($AutoResearchNoDemotion) {
        $AutoResearchArgs += "-NoDemotion"
    }
    if ($AutoResearchSpace.Trim()) {
        $AutoResearchArgs += @("-Space", $AutoResearchSpace)
    }
    if ($AutoResearchSeed -ge 0) {
        $AutoResearchArgs += @("-Seed", "$AutoResearchSeed")
    }

    Write-AutoResearchStartupLog -ConfigProfile $StackConfigProfile
    if ($AutoResearchOnce) {
        $AutoResearchScript = Join-Path $ScriptsRoot "start_autoresearch.ps1"
        Write-Host "[start_stack] running AutoResearch once in foreground"
        & $PowerShellExe -ExecutionPolicy Bypass -File $AutoResearchScript @AutoResearchArgs
        if ($LASTEXITCODE -ne 0) {
            throw "AutoResearch once failed with exit_code=$LASTEXITCODE"
        }
    } else {
        Start-RepoWindow -ScriptName "start_autoresearch.ps1" -ScriptArgs $AutoResearchArgs -Visible:$VisibleWindows
    }
}

Write-Host "[start_stack] repo=$RepoRoot"
Write-Host ("[start_stack] api={0} ui={1} bot={2} autoresearch={3} training_daemon={4}" -f $(-not $SkipApi), $(-not $SkipUi), $StackIncludesBot, $StackIncludesAutoResearch, $StackIncludesTrainingDaemon)
Write-Host "[start_stack] ui=http://127.0.0.1:5173"
Write-Host "[start_stack] api=http://$ApiHost`:$ApiPort/docs"
Write-Host "[start_stack] default login: viewer/viewer | operator/operator | admin/admin"
Write-Host "[start_stack] -IncludeBot launches bot dry-run + AutoResearch daemon + ML training daemon by default; add -SkipAutoResearch or -SkipTrainingDaemon to opt out"
Write-Host "[start_stack] startup preflight runs pytest, core report regeneration, isolated AutoResearch smoke, strategy gate, and Policy Center status unless -SkipStartupPreflight is used"
Write-Host "[start_stack] AutoResearch live promotion is forced off by the launcher"
