param(
    [switch]$IncludeBot,
    [switch]$SkipBot,
    [switch]$SkipAutoResearch,
    [switch]$SkipApi,
    [switch]$SkipUi,
    [switch]$BotRealMode,
    [switch]$UiInstallIfMissing,
    [switch]$AutoResearchOnce,
    [switch]$AutoResearchRegenerateReports,
    [switch]$AutoResearchSkipRegenerateReports,
    [switch]$AutoResearchNoPaperPromote,
    [switch]$AutoResearchNoDemotion,
    [string]$AutoResearchSpace = "",
    [int]$AutoResearchMaxCandidates = 3,
    [int]$AutoResearchMaxParallel = 1,
    [string]$AutoResearchMode = "seeded_random",
    [double]$AutoResearchIntervalHours = 6.0,
    [int]$AutoResearchSeed = -1,
    [string]$ApiHost = "127.0.0.1",
    [int]$ApiPort = 8000,
    [string]$UiApiProxyTarget = "http://127.0.0.1:8000",
    [int]$ApiReadyTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

if ($AutoResearchRegenerateReports -and $AutoResearchSkipRegenerateReports) {
    throw "Use either -AutoResearchRegenerateReports or -AutoResearchSkipRegenerateReports, not both."
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ScriptsRoot = Join-Path $RepoRoot "scripts"
$PowerShellExe = "powershell.exe"

function Start-RepoWindow {
    param(
        [string]$ScriptName,
        [string[]]$ScriptArgs
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

    Start-Process -FilePath $PowerShellExe -WorkingDirectory $RepoRoot -ArgumentList $Args | Out-Null
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
$StackIncludesAutoResearch = $StackIncludesBot -and -not $SkipAutoResearch
$AutoResearchShouldPrepareReports = $StackIncludesAutoResearch -and -not $AutoResearchSkipRegenerateReports

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

    $Python = Join-Path $Root ".venv\Scripts\python.exe"
    $Tool = Join-Path $Root "tools\regenerate_core_reports.py"
    if (-not (Test-Path $Python)) {
        throw "Project venv not found at $Python"
    }
    if (-not (Test-Path $Tool)) {
        throw "Core report regeneration tool not found at $Tool"
    }

    Write-Host "[start_stack] regenerating core reports before bot/autoresearch startup"
    & $Python $Tool
    if ($LASTEXITCODE -ne 0) {
        throw "Core report regeneration failed with exit_code=$LASTEXITCODE"
    }
}

if ($StackIncludesBot) {
    Clear-StaleBotLock -Root $RepoRoot
}

if ($AutoResearchShouldPrepareReports) {
    Invoke-CoreReportRegeneration -Root $RepoRoot
}

if (-not $SkipApi) {
    Start-RepoWindow -ScriptName "start_api.ps1" -ScriptArgs @("-BindHost", $ApiHost, "-Port", "$ApiPort")
}

if (-not $SkipUi) {
    $null = Wait-ApiReady -ApiBaseUrl $UiApiProxyTarget -TimeoutSeconds $ApiReadyTimeoutSeconds
    $UiArgs = @("-ApiProxyTarget", $UiApiProxyTarget)
    if ($UiInstallIfMissing) {
        $UiArgs += "-InstallIfMissing"
    }
    Start-RepoWindow -ScriptName "start_ui.ps1" -ScriptArgs $UiArgs
}

if ($StackIncludesBot) {
    $BotArgs = @()
    if ($BotRealMode) {
        $BotArgs += "-RealMode"
    }
    Start-RepoWindow -ScriptName "start_bot.ps1" -ScriptArgs $BotArgs

    if ($StackIncludesAutoResearch) {
        $AutoResearchArgs = @(
            "-MaxCandidates", "$AutoResearchMaxCandidates",
            "-MaxParallel", "$AutoResearchMaxParallel",
            "-Mode", $AutoResearchMode,
            "-IntervalHours", "$AutoResearchIntervalHours"
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
        Start-RepoWindow -ScriptName "start_autoresearch.ps1" -ScriptArgs $AutoResearchArgs
    }
}

Write-Host "[start_stack] repo=$RepoRoot"
Write-Host ("[start_stack] api={0} ui={1} bot={2} autoresearch={3}" -f $(-not $SkipApi), $(-not $SkipUi), $StackIncludesBot, $StackIncludesAutoResearch)
Write-Host "[start_stack] ui=http://127.0.0.1:5173"
Write-Host "[start_stack] api=http://$ApiHost`:$ApiPort/docs"
Write-Host "[start_stack] default login: viewer/viewer | operator/operator | admin/admin"
Write-Host "[start_stack] -IncludeBot launches bot dry-run + AutoResearch daemon by default"
Write-Host "[start_stack] core reports are prepared before startup and refreshed before AutoResearch cycles by default"
Write-Host "[start_stack] use -SkipAutoResearch with -IncludeBot if you only want the bot"
