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

if ($IncludeBot -and -not $SkipBot) {
    $BotArgs = @()
    if ($BotRealMode) {
        $BotArgs += "-RealMode"
    }
    Start-RepoWindow -ScriptName "start_bot.ps1" -ScriptArgs $BotArgs

    if (-not $SkipAutoResearch) {
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
Write-Host ("[start_stack] api={0} ui={1} bot={2} autoresearch={3}" -f $(-not $SkipApi), $(-not $SkipUi), $($IncludeBot -and -not $SkipBot), $($IncludeBot -and -not $SkipBot -and -not $SkipAutoResearch))
Write-Host "[start_stack] ui=http://127.0.0.1:5173"
Write-Host "[start_stack] api=http://$ApiHost`:$ApiPort/docs"
Write-Host "[start_stack] default login: viewer/viewer | operator/operator | admin/admin"
Write-Host "[start_stack] bot starts stopped by default; use -IncludeBot to launch bot + AutoResearch"
Write-Host "[start_stack] use -SkipAutoResearch with -IncludeBot if you only want the bot"
