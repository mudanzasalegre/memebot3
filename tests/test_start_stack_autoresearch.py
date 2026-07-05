from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_start_stack_include_bot_bootstraps_autoresearch_and_preflight() -> None:
    text = (ROOT / "scripts" / "start_stack.ps1").read_text(encoding="utf-8")

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
    assert 'Invoke-ProjectPython -Root $Root -PythonArgs @("-m", "pytest", "-q") -Label "pytest"' in text
    assert "Invoke-PolicyPreflightStatus" in text
    assert r'"tools\preflight.py"' in text
    assert '"--external-pytest-passed"' in text
    assert r'"tools\autoresearch_smoke.py", "--root", $SmokeRoot' in text
    assert r'"scripts\strategy_quality_gate.py", "--warn-only"' in text
    assert '"-IntervalHours", "$ResolvedAutoResearchIntervalHours"' in text
    assert '"-IntervalSeconds", "$TrainingDaemonIntervalSeconds"' in text
    assert 'Start-RepoWindow -ScriptName "start_training_daemon.ps1"' in text
    assert '$StartArgs.WindowStyle = "Hidden"' in text
    assert "AutoResearch process starting" in text
    assert "CONFIG_PROFILE=$ConfigProfile" in text
    assert "AUTORESEARCH_ENABLED=true" in text
    assert "AUTORESEARCH_MODE=paper_replay" in text
    assert "AUTORESEARCH_LIVE_PROMOTION_ENABLED=false" in text
    assert "AUTORESEARCH_AUTO_LIVE_PROMOTE=false" in text
    assert r"AutoResearch command: .\.venv\Scripts\python.exe tools\run_autoresearch_loop.py" in text
    assert "-IncludeBot launches bot dry-run + AutoResearch daemon + ML training daemon by default" in text


def test_start_bot_prepares_policy_preflight_status_when_missing() -> None:
    text = (ROOT / "scripts" / "start_bot.ps1").read_text(encoding="utf-8")

    assert "[switch]$SkipPolicyPreflightStatus" in text
    assert '$PreflightTool = Join-Path $RepoRoot "tools\\preflight.py"' in text
    assert '$PreflightStatus = Join-Path $RepoRoot "data\\metrics\\preflight_status.json"' in text
    assert "[start_bot] preparing Policy Center preflight status" in text


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

    assert "[int]$IntervalSeconds = 3600" in text
    assert '$env:ML_TRAINING_DAEMON_ENABLED = "true"' in text
    assert '$env:ML_TRAINING_DAEMON_INTERVAL_S = "$IntervalSeconds"' in text
    assert r"Training daemon command: .\.venv\Scripts\python.exe scripts\run_training_daemon.py" in text
    assert 'Write-Warning "[start_training_daemon] daemon exited unexpectedly; restarting in $RestartDelaySeconds seconds"' in text
