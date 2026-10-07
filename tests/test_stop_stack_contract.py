from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_stop_stack_defers_self_ancestors_until_final_leaf_stop() -> None:
    text = (ROOT / "scripts" / "stop_stack.ps1").read_text(encoding="utf-8")

    assert "function Get-ProcessAncestorMap" in text
    assert "param([int]$TargetProcessId)" in text
    assert "param([int]$Pid)" not in text
    assert "$selfAncestors = Get-ProcessAncestorMap -TargetProcessId $SelfPid" in text
    assert "function Get-TreeStopRoots" in text
    assert "function Get-AncestorStopTargets" in text
    assert "function Stop-ProcessLeaf" in text
    assert 'Stop-ProcessLeaf -TargetProcessId ([int]$target.Process.ProcessId) -Reason "repo_stack_ancestor"' in text
    assert '$TaskkillArgs = @("/PID", "$TargetProcessId")' in text
    assert '$TaskkillArgs += "/T"' in text
    assert 'Stop-VerifiedRepoProcess -TargetProcessId $TargetProcessId -Reason $Reason -Tree' in text
    assert 'Stop-VerifiedRepoProcess -TargetProcessId $TargetProcessId -Reason $Reason' in text
    assert "$selfAncestors.ContainsKey($TargetProcessId)" in text
    assert "non_ancestor_targets" in text


def test_stop_stack_covers_all_repo_stack_process_markers() -> None:
    text = (ROOT / "scripts" / "stop_stack.ps1").read_text(encoding="utf-8")

    for marker in (
        "scripts\\start_stack.ps1",
        "scripts\\start_api.ps1",
        "scripts\\start_ui.ps1",
        "scripts\\start_bot.ps1",
        "scripts\\start_autoresearch.ps1",
        "scripts\\start_training_daemon.ps1",
        "-m run_bot",
        "run_bot.py",
        "api.main:app",
        "tools\\run_autoresearch_loop.py",
        "scripts\\run_training_daemon.py",
        "run_training_daemon.py",
        "npm run dev",
        "vite",
    ):
        assert marker in text


def test_stop_stack_logs_remaining_processes_and_fails_when_not_clean() -> None:
    text = (ROOT / "scripts" / "stop_stack.ps1").read_text(encoding="utf-8")

    assert "$remaining = @(Get-RepoStackProcesses" in text
    assert "remaining pid=" in text
    assert 'Write-StopLog "stop_stack done killed=$($Killed.Count) remaining=$($Failures.Count)"' in text
    assert "if ($Failures.Count -gt 0)" in text
    assert "exit 1" in text


def test_stop_stack_default_escalates_only_for_same_verified_repo_target() -> None:
    text = (ROOT / "scripts" / "stop_stack.ps1").read_text(encoding="utf-8")

    assert "function Get-VerifiedRepoStackProcessById" in text
    assert "function Test-SameProcessIdentity" in text
    assert "function Invoke-TaskkillCommand" in text
    assert "Test-RepoStackProcess -Process $Process" in text
    assert '$GracefulExitCode = Invoke-TaskkillCommand' in text
    assert "$SameTargetRemaining = Test-SameProcessIdentity" in text
    assert "if ($SameTargetRemaining)" in text
    assert '$ForcedArgs = $TaskkillArgs + @("/F")' in text
    assert '-Stage "forced_retry"' in text
    assert "same_verified_target_remaining" in text
    assert "original_target_no_longer_present" in text
    assert "$SameTargetStillRunning" in text


def test_stop_stack_force_mode_remains_force_first() -> None:
    text = (ROOT / "scripts" / "stop_stack.ps1").read_text(encoding="utf-8")
    force_branch_start = text.index("    if ($Force) {")
    graceful_branch_start = text.index("    } else {", force_branch_start)
    force_branch = text[force_branch_start:graceful_branch_start]

    assert '$ForcedArgs = $TaskkillArgs + @("/F")' in force_branch
    assert '-Stage "forced"' in force_branch
    assert "GracefulExitCode" not in force_branch


def test_stop_stack_finalizes_runtime_state_and_removes_stale_lock_only_when_clean() -> None:
    text = (ROOT / "scripts" / "stop_stack.ps1").read_text(encoding="utf-8")

    assert 'if ($Failures.Count -eq 0)' in text
    assert 'finalize_stack_stop.py' in text
    assert '--requested-by $RequestedBy' in text
    assert r'data\run_bot.lock' in text
    assert 'removed stale bot lock after clean process shutdown' in text
