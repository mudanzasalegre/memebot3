from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_start_stack_include_bot_bootstraps_autoresearch_and_preflight() -> None:
    text = (ROOT / "scripts" / "start_stack.ps1").read_text(encoding="utf-8")

    assert '$DefaultConfigProfile = "paper_hotfix_0707"' in text
    assert '[Environment]::SetEnvironmentVariable("CONFIG_PROFILE_PATH", $null, "Process")' in text
    assert "clearing inherited CONFIG_PROFILE_PATH" in text
    assert "[switch]$IncludeAutoResearch" in text
    assert "[switch]$SkipStartupPreflight" in text
    assert "[switch]$SkipStartupTests" in text
    assert "[switch]$SkipStartupSmoke" in text
    assert "[switch]$SkipTrainingDaemon" in text
    assert "[switch]$VisibleWindows" in text
    assert "[int]$AutoResearchIntervalMinutes = 60" in text
    assert "[int]$TrainingDaemonIntervalSeconds = 3600" in text
    assert "$StackIncludesAutoResearch = ($IncludeAutoResearch -or $IncludeBot) -and -not $SkipAutoResearch" in text
    assert "$StackIncludesTrainingDaemon = $StackIncludesBot -and -not $SkipTrainingDaemon" in text
    assert "Invoke-StartupPreflight" in text
    assert '[string[]]$UnsetEnv = @()' in text
    assert '[Environment]::SetEnvironmentVariable($Name, $null, "Process")' in text
    assert '[Environment]::SetEnvironmentVariable($Name, $SavedEnv[$Name], "Process")' in text
    assert 'Invoke-ProjectPython -Root $Root -PythonArgs @("-m", "pytest", "-q") -Label "pytest" -UnsetEnv @("CONFIG_PROFILE", "CONFIG_PROFILE_PATH")' in text
    assert "Invoke-PolicyPreflightStatus" in text
    assert r'"tools\preflight.py"' in text
    assert '"--external-pytest-passed"' in text
    assert r'"tools\autoresearch_smoke.py", "--root", $SmokeRoot' in text
    assert r'"scripts\strategy_quality_gate.py", "--warn-only"' in text
    assert '"-IntervalHours", "$ResolvedAutoResearchIntervalHours"' in text
    assert '"-IntervalSeconds", "$TrainingDaemonIntervalSeconds"' in text
    assert 'Start-RepoWindow -ScriptName "start_training_daemon.ps1"' in text
    assert '$StartArgs.WindowStyle = "Hidden"' in text
    assert 'if ($Visible) {' in text
    assert '$Args = @("-NoExit") + $Args' in text
    assert 'Stop-StartedProcessTree -Process $ApiProcess -Reason "api_readiness_failed"' in text
    assert '$ApiReady = Wait-ApiReady -ApiBaseUrl $ApiReadinessBaseUrl' in text
    assert 'Stop-StartedProcessTree -Process $ApiProcess -Reason "ui_api_proxy_readiness_failed"' in text
    assert 'throw "API readiness failed; UI and remaining stack services were not started"' in text
    assert "AutoResearch process starting" in text
    assert "CONFIG_PROFILE=$ConfigProfile" in text
    assert "AUTORESEARCH_ENABLED=true" in text
    assert "AUTORESEARCH_MODE=paper_replay" in text
    assert "AUTORESEARCH_LIVE_PROMOTION_ENABLED=false" in text
    assert "AUTORESEARCH_AUTO_LIVE_PROMOTE=false" in text
    assert r"AutoResearch command: .\.venv\Scripts\python.exe tools\run_autoresearch_loop.py" in text
    assert "-IncludeBot launches bot dry-run + AutoResearch daemon + ML training daemon by default" in text


def test_start_stack_reconciles_runtime_before_removing_stale_bot_lock() -> None:
    text = (ROOT / "scripts" / "start_stack.ps1").read_text(encoding="utf-8")
    function_start = text.index("function Clear-StaleBotLock")
    function_end = text.index("function Invoke-CoreReportRegeneration", function_start)
    clear_lock_body = text[function_start:function_end]

    assert "function Invoke-StaleBotLockReconciliation" in text
    assert '"scripts\\finalize_stack_stop.py"' in text
    assert '--requested-by "start_stack_stale_lock" --require-runtime-state' in text
    assert "$ExitCode = $LASTEXITCODE" in text
    assert "lock was preserved" in text
    assert clear_lock_body.index("Invoke-StaleBotLockReconciliation -Root $Root") < clear_lock_body.index(
        "Remove-Item -LiteralPath $LockPath -Force"
    )
    assert "throw \"Could not remove stale bot lock after runtime reconciliation" in clear_lock_body


def test_start_bot_prepares_policy_preflight_status_when_missing() -> None:
    text = (ROOT / "scripts" / "start_bot.ps1").read_text(encoding="utf-8")

    assert '$DefaultConfigProfile = "paper_hotfix_0707"' in text
    assert '[Environment]::SetEnvironmentVariable("CONFIG_PROFILE_PATH", $null, "Process")' in text
    assert "clearing inherited CONFIG_PROFILE_PATH" in text
    assert "[switch]$SkipPolicyPreflightStatus" in text
    assert '$PreflightTool = Join-Path $RepoRoot "tools\\preflight.py"' in text
    assert '$PreflightStatus = Join-Path $RepoRoot "data\\metrics\\preflight_status.json"' in text
    assert "[start_bot] preparing Policy Center preflight status" in text
    assert "[start_bot] requested_mode=$RequestedMode" in text
    assert "[start_bot] effective_mode=$EffectiveMode" in text
    assert "the selected profile enforces dry-run" in text


def test_start_stack_fails_fast_before_ui_when_api_is_unreachable() -> None:
    env = os.environ.copy()
    env["CONFIG_PROFILE"] = "paper_hotfix_0707"
    env["CONFIG_PROFILE_PATH"] = str(ROOT / "stale-profile-that-must-not-win.env")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "start_stack.ps1"),
            "-SkipApi",
            "-SkipStartupPreflight",
            "-ApiReadyTimeoutSeconds",
            "1",
            "-UiApiProxyTarget",
            "http://127.0.0.1:1",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "config_profile=paper_hotfix_0707" in output
    assert "clearing inherited CONFIG_PROFILE_PATH" in output
    assert "API readiness failed; UI and remaining stack services were not started" in output
    assert "started start_ui.ps1" not in output


def test_start_stack_powershell_smoke_exists() -> None:
    text = (ROOT / "tests" / "test_start_stack_autoresearch.ps1").read_text(encoding="utf-8")

    assert "-IncludeBot" in text
    assert "-SkipBot" in text
    assert "-AutoResearchOnce" in text
    assert "autoresearch_runtime_state.json" in text
    assert "autoresearch_events.jsonl" in text
    assert "acquisition_health_report.json" in text
    assert "current_run_autotune_state.json" in text


def test_start_autoresearch_forces_safe_runtime_env() -> None:
    text = (ROOT / "scripts" / "start_autoresearch.ps1").read_text(encoding="utf-8")

    assert '$DefaultConfigProfile = "paper_hotfix_0707"' in text
    assert '$env:CONFIG_PROFILE = $StackConfigProfile' in text
    assert '$env:AUTORESEARCH_ENABLED = "true"' in text
    assert '$env:AUTORESEARCH_MODE = "paper_replay"' in text
    assert '$env:AUTORESEARCH_LIVE_PROMOTION_ENABLED = "false"' in text
    assert '$env:AUTORESEARCH_AUTO_LIVE_PROMOTE = "false"' in text
    assert '$env:AUTO_PROMOTE_LIVE = "false"' in text
    assert '$env:MODEL_AUTO_PROMOTE = "false"' in text
    assert '$env:ML_AUTO_PROMOTE_LANES = "false"' in text


def test_start_autoresearch_does_not_regenerate_reports_by_default() -> None:
    text = (ROOT / "scripts" / "start_autoresearch.ps1").read_text(encoding="utf-8")

    assert "$ShouldRegenerateReports = $RegenerateReports -and (-not $SkipRegenerateReports)" in text
    assert 'if ($ShouldRegenerateReports) {' in text
    assert '$Args += "--regenerate-reports"' in text


def test_start_autoresearch_supervises_daemon_mode() -> None:
    text = (ROOT / "scripts" / "start_autoresearch.ps1").read_text(encoding="utf-8")

    assert "[int]$RestartDelaySeconds = 30" in text
    assert "[int]$MaxRestarts = 0" in text
    assert 'if ($Once) {' in text
    assert 'Write-Warning "[start_autoresearch] daemon exited unexpectedly; restarting in $RestartDelaySeconds seconds"' in text
    assert "Start-Sleep -Seconds $RestartDelaySeconds" in text


def test_start_training_daemon_exists_and_runs_hourly_with_supervisor() -> None:
    text = (ROOT / "scripts" / "start_training_daemon.ps1").read_text(encoding="utf-8")

    assert '$DefaultConfigProfile = "paper_hotfix_0707"' in text
    assert "[int]$IntervalSeconds = 3600" in text
    assert '$env:ML_TRAINING_DAEMON_ENABLED = "true"' in text
    assert '$env:ML_TRAINING_DAEMON_INTERVAL_S = "$IntervalSeconds"' in text
    assert r"Training daemon command: .\.venv\Scripts\python.exe scripts\run_training_daemon.py" in text
    assert 'Write-Warning "[start_training_daemon] daemon exited unexpectedly; restarting in $RestartDelaySeconds seconds"' in text
