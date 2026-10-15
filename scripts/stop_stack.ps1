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
$Failures = New-Object System.Collections.Generic.List[string]

function Write-StopLog {
    param([string]$Message)
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    Add-Content -Path $LogPath -Value "[$stamp] $Message" -Encoding UTF8
}

function Get-ProcessAncestorMap {
    param([int]$TargetProcessId)

    $ancestors = @{}
    $currentPid = $TargetProcessId
    while ($currentPid -gt 0) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$currentPid" -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            break
        }
        $parentPid = [int]$process.ParentProcessId
        if ($parentPid -le 0 -or $parentPid -eq $currentPid -or $ancestors.ContainsKey($parentPid)) {
            break
        }
        $ancestors[$parentPid] = $true
        $currentPid = $parentPid
    }
    return $ancestors
}

function Test-RepoStackProcess {
    param(
        [object]$Process,
        [string]$EscapedRoot,
        [string[]]$ScriptMarkers,
        [string[]]$RuntimeMarkers
    )

    $cmd = [string]$Process.CommandLine
    if (-not $cmd -or [int]$Process.ProcessId -eq $SelfPid -or -not ($cmd -match $EscapedRoot)) {
        return $false
    }
    foreach ($marker in $ScriptMarkers) {
        if ($cmd -like "*$marker*") {
            return $true
        }
    }
    foreach ($marker in $RuntimeMarkers) {
        if ($cmd -like "*$marker*") {
            return $true
        }
    }
    return $false
}

function Get-RepoStackProcesses {
    param(
        [string]$EscapedRoot,
        [string[]]$ScriptMarkers,
        [string[]]$RuntimeMarkers
    )

    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        Test-RepoStackProcess -Process $_ -EscapedRoot $EscapedRoot -ScriptMarkers $ScriptMarkers -RuntimeMarkers $RuntimeMarkers
    }
}

function Get-VerifiedRepoStackProcessById {
    param([int]$TargetProcessId)

    if ($TargetProcessId -le 0 -or $TargetProcessId -eq $SelfPid) {
        return $null
    }
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId=$TargetProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $Process) {
        return $null
    }
    if (-not (Test-RepoStackProcess -Process $Process -EscapedRoot $escapedRoot -ScriptMarkers $scriptMarkers -RuntimeMarkers $runtimeMarkers)) {
        return $null
    }
    return $Process
}

function Test-SameProcessIdentity {
    param(
        [object]$InitialProcess,
        [object]$CurrentProcess
    )

    if ($null -eq $InitialProcess -or $null -eq $CurrentProcess) {
        return $false
    }
    if ([int]$InitialProcess.ProcessId -ne [int]$CurrentProcess.ProcessId) {
        return $false
    }
    if ([string]$InitialProcess.CommandLine -ne [string]$CurrentProcess.CommandLine) {
        return $false
    }
    $InitialCreatedAt = [string]$InitialProcess.CreationDate
    $CurrentCreatedAt = [string]$CurrentProcess.CreationDate
    if ($InitialCreatedAt -and $CurrentCreatedAt -and $InitialCreatedAt -ne $CurrentCreatedAt) {
        return $false
    }
    return $true
}

function Invoke-TaskkillCommand {
    param(
        [int]$TargetProcessId,
        [string[]]$TaskkillArgs,
        [string]$Stage
    )

    $Output = @(& taskkill.exe @TaskkillArgs 2>&1)
    $ExitCode = $LASTEXITCODE
    foreach ($Line in $Output) {
        if (-not [string]::IsNullOrWhiteSpace([string]$Line)) {
            Write-StopLog "taskkill pid=$TargetProcessId stage=$Stage exit_code=$ExitCode output=$([string]$Line)"
        }
    }
    return [int]$ExitCode
}

function Invoke-CooperativeStopHelper {
    param([string]$Python, [string]$Requester, [int]$OwnerPid)

    # The helper's 15s clock cannot interrupt a stuck filesystem operation.
    # Independently bound its process to20s, retaining the original creation
    # identity before any helper-tree termination (including a venv launcher).
    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $Python
    $StartInfo.Arguments = '"' + $Requester + '" --expected-pid ' + $OwnerPid + ' --timeout 15'
    $StartInfo.WorkingDirectory = $RepoRoot
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $Helper = [System.Diagnostics.Process]::Start($StartInfo)
    try {
        Write-StopLog "cooperative_phase=helper_started helper_pid=$($Helper.Id)"
        $InitialHelper = Get-CimInstance Win32_Process -Filter "ProcessId=$($Helper.Id)" -ErrorAction SilentlyContinue
        Write-StopLog "cooperative_phase=helper_identity_read_complete helper_pid=$($Helper.Id) observed=$($null -ne $InitialHelper)"
        $OutputTask = $Helper.StandardOutput.ReadToEndAsync()
        $ErrorTask = $Helper.StandardError.ReadToEndAsync()
        if (-not $Helper.WaitForExit(20000)) {
            Write-StopLog "cooperative_stop helper_outer_timeout_seconds=20; cleanup unconfirmed"
            $CurrentHelper = Get-CimInstance Win32_Process -Filter "ProcessId=$($Helper.Id)" -ErrorAction SilentlyContinue
            if ((-not $Helper.HasExited) -and (Test-SameProcessIdentity -InitialProcess $InitialHelper -CurrentProcess $CurrentHelper) -and ([string]$CurrentHelper.CommandLine -like "*$Requester*")) {
                $HelperArgs = @("/PID", "$($Helper.Id)", "/T", "/F")
                [void](Invoke-TaskkillCommand -TargetProcessId $Helper.Id -TaskkillArgs $HelperArgs -Stage "cooperative_helper_timeout")
                [void]$Helper.WaitForExit(2000)
            }
        }
        foreach ($ReadTask in @($OutputTask, $ErrorTask)) {
            if ($ReadTask.Wait(1000)) {
                foreach ($Line in ($ReadTask.Result -split "`r?`n")) {
                    if (-not [string]::IsNullOrWhiteSpace($Line)) { Write-StopLog "cooperative_stop $Line" }
                }
            }
        }
        if ($Helper.HasExited) {
            Write-StopLog "cooperative_stop helper_exit_code=$($Helper.ExitCode); remaining targets use verified fallback"
        }
    } finally {
        $Helper.Dispose()
    }
}

function Stop-VerifiedRepoProcess {
    param(
        [int]$TargetProcessId,
        [string]$Reason,
        [switch]$Tree
    )

    if ($TargetProcessId -le 0 -or $TargetProcessId -eq $SelfPid -or $Killed.Contains($TargetProcessId)) {
        return
    }
    $InitialProcess = Get-VerifiedRepoStackProcessById -TargetProcessId $TargetProcessId
    if ($null -eq $InitialProcess) {
        Write-StopLog "skipped pid=$TargetProcessId reason=$Reason because target is no longer verified repo-scoped"
        return
    }

    $Mode = if ($Tree) { "tree" } else { "leaf" }
    $TaskkillArgs = @("/PID", "$TargetProcessId")
    if ($Tree) {
        $TaskkillArgs += "/T"
    }
    Write-StopLog "stopping pid=$TargetProcessId reason=$Reason mode=$Mode force=$($Force.IsPresent)"

    if ($Force) {
        $ForcedArgs = $TaskkillArgs + @("/F")
        $ForcedExitCode = Invoke-TaskkillCommand -TargetProcessId $TargetProcessId -TaskkillArgs $ForcedArgs -Stage "forced"
        Write-StopLog "taskkill_result pid=$TargetProcessId mode=$Mode stage=forced exit_code=$ForcedExitCode"
    } else {
        $GracefulExitCode = Invoke-TaskkillCommand -TargetProcessId $TargetProcessId -TaskkillArgs $TaskkillArgs -Stage "graceful"
        Start-Sleep -Milliseconds 200

        $RemainingProcess = Get-VerifiedRepoStackProcessById -TargetProcessId $TargetProcessId
        $SameTargetRemaining = $false
        if ($null -ne $RemainingProcess) {
            $SameTargetRemaining = Test-SameProcessIdentity -InitialProcess $InitialProcess -CurrentProcess $RemainingProcess
        }

        if ($SameTargetRemaining) {
            Write-StopLog "taskkill escalating pid=$TargetProcessId mode=$Mode graceful_exit_code=$GracefulExitCode reason=same_verified_target_remaining"
            $ForcedArgs = $TaskkillArgs + @("/F")
            $ForcedExitCode = Invoke-TaskkillCommand -TargetProcessId $TargetProcessId -TaskkillArgs $ForcedArgs -Stage "forced_retry"
            Write-StopLog "taskkill_result pid=$TargetProcessId mode=$Mode stage=forced_retry exit_code=$ForcedExitCode"
        } elseif ($GracefulExitCode -ne 0) {
            Write-StopLog "taskkill not escalating pid=$TargetProcessId mode=$Mode graceful_exit_code=$GracefulExitCode reason=original_target_no_longer_present"
        }
    }

    Start-Sleep -Milliseconds 100
    $FinalProcess = Get-VerifiedRepoStackProcessById -TargetProcessId $TargetProcessId
    $SameTargetStillRunning = $false
    if ($null -ne $FinalProcess) {
        $SameTargetStillRunning = Test-SameProcessIdentity -InitialProcess $InitialProcess -CurrentProcess $FinalProcess
    }
    if ($SameTargetStillRunning) {
        Write-StopLog "taskkill target still running pid=$TargetProcessId mode=$Mode reason=$Reason"
        return
    }
    [void]$Killed.Add($TargetProcessId)
}

function Stop-ProcessTree {
    param([int]$TargetProcessId, [string]$Reason)

    if ($null -ne $selfAncestors -and $selfAncestors.ContainsKey($TargetProcessId)) {
        Write-StopLog "deferred tree stop pid=$TargetProcessId because it is an ancestor of stop_stack"
        return
    }
    Stop-VerifiedRepoProcess -TargetProcessId $TargetProcessId -Reason $Reason -Tree
}

function Stop-ProcessLeaf {
    param([int]$TargetProcessId, [string]$Reason)

    Stop-VerifiedRepoProcess -TargetProcessId $TargetProcessId -Reason $Reason
}

function Get-TreeStopRoots {
    param(
        [object[]]$Processes,
        [hashtable]$AncestorMap
    )

    $targetIds = @{}
    $byId = @{}
    foreach ($process in $Processes) {
        $pidValue = [int]$process.ProcessId
        $targetIds[$pidValue] = $true
        $byId[$pidValue] = $process
    }

    $roots = @()
    foreach ($process in $Processes) {
        $pidValue = [int]$process.ProcessId
        if ($AncestorMap.ContainsKey($pidValue)) {
            continue
        }

        $hasTargetParent = $false
        $parentPid = [int]$process.ParentProcessId
        while ($parentPid -gt 0) {
            if ($targetIds.ContainsKey($parentPid) -and -not $AncestorMap.ContainsKey($parentPid)) {
                $hasTargetParent = $true
                break
            }
            if (-not $byId.ContainsKey($parentPid)) {
                break
            }
            $parentPid = [int]$byId[$parentPid].ParentProcessId
        }

        if (-not $hasTargetParent) {
            $roots += $process
        }
    }
    return $roots
}

function Get-AncestorStopTargets {
    param(
        [object[]]$Processes,
        [hashtable]$AncestorMap
    )

    $targets = @()
    foreach ($process in $Processes) {
        $pidValue = [int]$process.ProcessId
        if ($AncestorMap.ContainsKey($pidValue)) {
            $depth = 0
            $parentPid = [int]$process.ParentProcessId
            while ($parentPid -gt 0 -and $AncestorMap.ContainsKey($parentPid)) {
                $depth += 1
                $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$parentPid" -ErrorAction SilentlyContinue
                if ($null -eq $parent) {
                    break
                }
                $parentPid = [int]$parent.ParentProcessId
            }
            $targets += [pscustomobject]@{
                Process = $process
                Depth = $depth
            }
        }
    }
    return $targets | Sort-Object Depth -Descending
}

Write-StopLog "stop_stack requested_by=$RequestedBy force=$($Force.IsPresent) repo=$RepoRoot"

$escapedRoot = [regex]::Escape($RepoRoot)
$scriptMarkers = @(
    "scripts\start_stack.ps1",
    "scripts\start_api.ps1",
    "scripts\start_ui.ps1",
    "scripts\start_bot.ps1",
    "scripts\start_autoresearch.ps1",
    "scripts\start_training_daemon.ps1",
    "scripts\request_runtime_stop.py"
)
$runtimeMarkers = @(
    "-m run_bot",
    "run_bot.py",
    "api.main:app",
    "tools\run_autoresearch_loop.py",
    "run_autoresearch_loop.py",
    "scripts\run_training_daemon.py",
    "run_training_daemon.py",
    "npm run dev",
    "vite"
)

$selfAncestors = Get-ProcessAncestorMap -TargetProcessId $SelfPid
Write-StopLog "stop_stack self_pid=$SelfPid ancestor_pids=$($selfAncestors.Keys -join ',')"

# Let the native runtime drain owned IO before any tree/taskkill operation.
# Force remains emergency force-first. Old/unsupported owners retain the
# existing command-line + creation-identity-verified fallback below.
if (-not $Force) {
    $StopPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $StopRequester = Join-Path $PSScriptRoot "request_runtime_stop.py"
    $OwnerPath = Join-Path $RepoRoot "data\run_bot.lock"
    Write-StopLog "cooperative_phase=owner_paths_begin"
    if ((Test-Path $StopPython) -and (Test-Path $StopRequester) -and (Test-Path $OwnerPath)) {
        try {
            Write-StopLog "cooperative_phase=owner_read_begin"
            $OwnerFile = Get-Item -LiteralPath $OwnerPath -ErrorAction Stop
            if ($OwnerFile.Length -gt 4096 -or $OwnerFile.PSIsContainer -or ($OwnerFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Owner receipt is not a bounded regular file"
            }
            $Owner = Get-Content -LiteralPath $OwnerPath -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
            $OwnerPid = [int]$Owner.pid
            Write-StopLog "cooperative_phase=owner_read_complete owner_pid=$OwnerPid"
            $OwnerProcess = Get-VerifiedRepoStackProcessById -TargetProcessId $OwnerPid
            if ($null -ne $OwnerProcess -and (([string]$OwnerProcess.CommandLine -like "*-m run_bot*") -or ([string]$OwnerProcess.CommandLine -like "*run_bot.py*"))) {
                Write-StopLog "cooperative_phase=owner_verified owner_pid=$OwnerPid"
                Write-StopLog "cooperative_stop requesting verified_runtime_pid=$OwnerPid timeout_seconds=15"
                Invoke-CooperativeStopHelper -Python $StopPython -Requester $StopRequester -OwnerPid $OwnerPid
            } else {
                Write-StopLog "cooperative_stop skipped: no verified native runtime owner"
            }
        } catch {
            Write-StopLog "cooperative_stop unavailable; retaining verified fallback"
        }
    }
}

if (Test-Path $StatePath) {
    try {
        $state = Get-Content -Path $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $managedPid = [int]($state.pid)
        if ($selfAncestors.ContainsKey($managedPid)) {
            Write-StopLog "deferred managed pid=$managedPid because it is an ancestor of stop_stack"
        } else {
            Stop-ProcessTree -TargetProcessId $managedPid -Reason "ui_managed_bot"
        }
        Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
    } catch {
        Write-StopLog "managed state read failed: $($_.Exception.Message)"
    }
}

for ($round = 1; $round -le 3; $round += 1) {
    $processes = @(Get-RepoStackProcesses -EscapedRoot $escapedRoot -ScriptMarkers $scriptMarkers -RuntimeMarkers $runtimeMarkers)
    $treeRoots = @(Get-TreeStopRoots -Processes $processes -AncestorMap $selfAncestors)
    if ($treeRoots.Count -eq 0) {
        Write-StopLog "round=$round non_ancestor_targets=0"
        break
    }
    Write-StopLog "round=$round non_ancestor_targets=$($treeRoots.Count)"
    foreach ($process in ($treeRoots | Sort-Object ProcessId -Descending)) {
        Stop-ProcessTree -TargetProcessId ([int]$process.ProcessId) -Reason "repo_stack"
    }
    Start-Sleep -Milliseconds 750
}

$remainingBeforeAncestors = @(Get-RepoStackProcesses -EscapedRoot $escapedRoot -ScriptMarkers $scriptMarkers -RuntimeMarkers $runtimeMarkers)
$ancestorTargets = @(Get-AncestorStopTargets -Processes $remainingBeforeAncestors -AncestorMap $selfAncestors)
foreach ($target in $ancestorTargets) {
    Stop-ProcessLeaf -TargetProcessId ([int]$target.Process.ProcessId) -Reason "repo_stack_ancestor"
}

Start-Sleep -Milliseconds 500
$remaining = @(Get-RepoStackProcesses -EscapedRoot $escapedRoot -ScriptMarkers $scriptMarkers -RuntimeMarkers $runtimeMarkers)
foreach ($process in $remaining) {
    $message = "remaining pid=$($process.ProcessId) parent=$($process.ParentProcessId) name=$($process.Name) cmd=$($process.CommandLine)"
    [void]$Failures.Add($message)
    Write-StopLog $message
}

if ($Failures.Count -eq 0) {
    $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $Finalizer = Join-Path $PSScriptRoot "finalize_stack_stop.py"
    if (-not (Test-Path $Python) -or -not (Test-Path $Finalizer)) {
        $message = "runtime finalizer unavailable python=$Python script=$Finalizer"
        [void]$Failures.Add($message)
        Write-StopLog $message
    } else {
        & $Python $Finalizer --requested-by $RequestedBy | ForEach-Object {
            Write-StopLog "runtime_finalizer $_"
        }
        if ($LASTEXITCODE -ne 0) {
            $message = "runtime finalizer failed exit_code=$LASTEXITCODE"
            [void]$Failures.Add($message)
            Write-StopLog $message
        }
    }

    if ($Failures.Count -eq 0) {
        $BotLockPath = Join-Path $RepoRoot "data\run_bot.lock"
        if (Test-Path $BotLockPath) {
            Remove-Item -LiteralPath $BotLockPath -Force -ErrorAction SilentlyContinue
            if (Test-Path $BotLockPath) {
                $message = "stale bot lock could not be removed: $BotLockPath"
                [void]$Failures.Add($message)
                Write-StopLog $message
            } else {
                Write-StopLog "removed stale bot lock after clean process shutdown"
            }
        }
    }
}

Write-StopLog "stop_stack done killed=$($Killed.Count) remaining=$($Failures.Count)"
if ($Failures.Count -gt 0) {
    exit 1
}
