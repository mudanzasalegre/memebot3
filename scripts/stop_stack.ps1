param(
    [switch]$Force,
    [int]$DelaySeconds = 0,
    [string]$RequestedBy = "ui"
)

$ErrorActionPreference = "SilentlyContinue"

if ($DelaySeconds -gt 0) {
    Start-Sleep -Seconds $DelaySeconds
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogPath = Join-Path $LogDir "stop_stack.log"
$StatePath = Join-Path $RepoRoot "data\runtime\ui_managed_bot_process.json"
$SelfPid = $PID
$Killed = New-Object System.Collections.Generic.HashSet[int]

function Write-StopLog {
    param([string]$Message)
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    Add-Content -Path $LogPath -Value "[$stamp] $Message" -Encoding UTF8
}

function Stop-ProcessTree {
    param([int]$Pid, [string]$Reason)
    if ($Pid -le 0 -or $Pid -eq $SelfPid -or $Killed.Contains($Pid)) {
        return
    }
    $proc = Get-Process -Id $Pid -ErrorAction SilentlyContinue
    if ($null -eq $proc) {
        return
    }
    [void]$Killed.Add($Pid)
    Write-StopLog "stopping pid=$Pid reason=$Reason"
    $args = @("/PID", "$Pid", "/T")
    if ($Force) {
        $args += "/F"
    }
    & taskkill @args | Out-Null
}

Write-StopLog "stop_stack requested_by=$RequestedBy force=$($Force.IsPresent) repo=$RepoRoot"

if (Test-Path $StatePath) {
    try {
        $state = Get-Content -Path $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $managedPid = [int]($state.pid)
        Stop-ProcessTree -Pid $managedPid -Reason "ui_managed_bot"
        Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
    } catch {
        Write-StopLog "managed state read failed: $($_.Exception.Message)"
    }
}

$escapedRoot = [regex]::Escape($RepoRoot)
$scriptMarkers = @(
    "scripts\start_stack.ps1",
    "scripts\start_api.ps1",
    "scripts\start_ui.ps1",
    "scripts\start_bot.ps1",
    "scripts\start_autoresearch.ps1",
    "scripts\start_training_daemon.ps1"
)
$runtimeMarkers = @(
    "-m run_bot",
    "run_bot.py",
    "api.main:app",
    "tools\run_autoresearch_loop.py",
    "run_autoresearch_loop.py",
    "run_training_daemon.py",
    "npm run dev",
    "vite"
)

$processes = Get-CimInstance Win32_Process | Where-Object {
    $cmd = [string]$_.CommandLine
    $_.ProcessId -ne $SelfPid -and
    $cmd -and
    ($cmd -match $escapedRoot) -and
    (
        ($scriptMarkers | Where-Object { $cmd -like "*$_*" }).Count -gt 0 -or
        ($runtimeMarkers | Where-Object { $cmd -like "*$_*" }).Count -gt 0
    )
}

foreach ($process in ($processes | Sort-Object ProcessId -Descending)) {
    Stop-ProcessTree -Pid ([int]$process.ProcessId) -Reason "repo_stack"
}

Write-StopLog "stop_stack done killed=$($Killed.Count)"
