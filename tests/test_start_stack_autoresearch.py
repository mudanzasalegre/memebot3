from __future__ import annotations

import os
from pathlib import Path
import subprocess
import shutil
import time

import pytest


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


def _await_native_startup_exit(process, stdout_path, stderr_path, *,
        startup_timeout_s=90, readiness_timeout_s=15, monotonic=time.monotonic, sleep=time.sleep):
    """Host shell startup is not the configured API health-check budget."""
    milestones = []
    started = monotonic()
    def output():
        return '\n'.join(path.read_text(encoding='utf-8',errors='replace')
            if path.exists() else '' for path in (stdout_path,stderr_path))
    for phase, timeout, complete in (
        ('host_startup',startup_timeout_s,lambda:'[start_stack] config_profile=' in output()),
        ('api_failure_delivery',readiness_timeout_s,lambda:process.poll() is not None),
    ):
        phase_started = monotonic()
        while not complete():
            elapsed = monotonic()-phase_started
            if elapsed>=timeout or phase=='host_startup' and process.poll() is not None:
                raise AssertionError(f'Startup milestone failed phase={phase} timeout_s={timeout} '
                    f'phase_elapsed_s={elapsed:.3f} total_elapsed_s={monotonic()-started:.3f} '
                    f'exit_code={process.poll()} milestones={milestones} output={output()[-4096:]}')
            sleep(.02)
        milestones.append({'phase':phase,'seconds':monotonic()-phase_started})
    return milestones


@pytest.mark.parametrize('host_start_delay_s',[0,20])
def test_start_stack_fails_fast_before_ui_when_api_is_unreachable(tmp_path,host_start_delay_s) -> None:
    launcher = shutil.which("powershell.exe") or shutil.which("pwsh")
    assert launcher is not None, "PowerShell is required for the native fail-fast startup test"
    isolated = tmp_path/'isolated-startup'
    scripts = isolated/'scripts'
    scripts.mkdir(parents=True)
    source = (ROOT/'scripts/start_stack.ps1').read_text(encoding='utf-8')
    if host_start_delay_s:
        anchor = 'Write-Host "[start_stack] config_profile=$StackConfigProfile"'
        assert source.count(anchor)==1
        source = source.replace(anchor,f'Start-Sleep -Seconds {host_start_delay_s}\n'+anchor)
    (scripts/'start_stack.ps1').write_text(source,encoding='utf-8')
    # A defect must not start any real UI/bot/training/research/API child.
    for name in ('start_ui','start_bot','start_training_daemon','start_autoresearch','start_api'):
        (scripts/(name+'.ps1')).write_text(
            f"New-Item -ItemType File -Path (Join-Path $PSScriptRoot '{name}-unexpected')\n"
            "throw 'Unexpected service dispatch in provider-free startup QA'\n",encoding='utf-8')
    keep = {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','LOCALAPPDATA',
        'APPDATA','HOMEDRIVE','HOMEPATH','COMSPEC','PATHEXT'}
    env = {key:value for key,value in os.environ.items() if key.upper() in keep}
    env["CONFIG_PROFILE"] = "paper_hotfix_0707"
    env["CONFIG_PROFILE_PATH"] = str(isolated / "stale-profile-that-must-not-win.env")
    command = [
            launcher,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts / "start_stack.ps1"),
            "-SkipApi",
            "-SkipStartupPreflight",
            "-ApiReadyTimeoutSeconds",
            "1",
            "-UiApiProxyTarget",
            "http://127.0.0.1:1",
        ]
    stdout_path,stderr_path = tmp_path/'stdout.log',tmp_path/'stderr.log'
    with stdout_path.open('wb') as stdout,stderr_path.open('wb') as stderr:
        process = subprocess.Popen(command,cwd=isolated,env=env,stdin=subprocess.DEVNULL,
            stdout=stdout,stderr=stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            milestones = _await_native_startup_exit(process,stdout_path,stderr_path)
        finally:
            if process.poll() is None:
                process.kill()  # Exact fixture shell handle, never a discovered PID.
            process.wait(timeout=10)
    result = subprocess.CompletedProcess(command,process.returncode,
        stdout_path.read_text(encoding='utf-8',errors='replace'),
        stderr_path.read_text(encoding='utf-8',errors='replace'))

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "config_profile=paper_hotfix_0707" in output
    assert "clearing inherited CONFIG_PROFILE_PATH" in output
    assert "API readiness failed; UI and remaining stack services were not started" in output
    assert "started start_ui.ps1" not in output
    assert [item['phase'] for item in milestones]==['host_startup','api_failure_delivery']
    assert milestones[0]['seconds']>=host_start_delay_s
    assert milestones[1]['seconds']<=15
    assert not list(scripts.glob('*-unexpected'))


@pytest.mark.parametrize('phase',['host_startup','api_failure_delivery'])
def test_startup_milestone_timeout_reports_the_exact_unfinished_phase(tmp_path,phase):
    stdout_path,stderr_path = tmp_path/'stdout',tmp_path/'stderr'
    stdout_path.write_text('[start_stack] config_profile=test' if phase=='api_failure_delivery' else '')
    stderr_path.write_text('original-stderr')
    class Alive:
        def poll(self): return None
    now = [0.]
    def sleep(seconds): now[0]+=seconds
    with pytest.raises(AssertionError,match='phase='+phase) as exc:
        _await_native_startup_exit(Alive(),stdout_path,stderr_path,startup_timeout_s=.1,
            readiness_timeout_s=.1,monotonic=lambda:now[0],sleep=sleep)
    assert 'original-stderr' in str(exc.value) and 'exit_code=None' in str(exc.value)


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
