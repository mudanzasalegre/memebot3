"""Native preflight command budgets and failure reporting, no bot start."""
import json
import subprocess
import sys

import pytest

from tools import preflight


def stub_supporting_checks(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    monkeypatch.setattr(preflight, "CRITICAL_MODULES", [])
    monkeypatch.setattr(preflight, "PAPER_HOTFIX_PROFILE", tmp_path / "profile.env")
    monkeypatch.setattr(preflight, "validate_paper_hotfix_profile", lambda path: {"ok": True})
    monkeypatch.setattr(preflight, "_report_dry_run_checks", lambda: [{"name": "synthetic", "ok": True}])


def test_timeout_is_reported_instead_of_escaping(monkeypatch):
    def expires(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"],
            output=b"x" * 5000, stderr=b"synthetic timeout")
    monkeypatch.setattr(preflight.subprocess, "run", expires)
    result = preflight._run(["synthetic-command"], timeout=17)
    assert result["returncode"] == 124 and result["timed_out"] is True
    assert result["timeout_seconds"] == 17
    assert len(result["stdout_tail"]) == 4000
    assert "synthetic timeout" in result["stderr_tail"]


def test_native_full_test_budget_exceeds_old_four_minutes(monkeypatch, tmp_path):
    stub_supporting_checks(monkeypatch, tmp_path)
    observed = []
    def run(cmd, *, timeout=120):
        if cmd[1:3] == ["-m", "pytest"]:
            observed.append(timeout)
        return {"cmd": cmd, "returncode": 0, "stdout_tail": "", "stderr_tail": ""}
    monkeypatch.setattr(preflight, "_run", run)
    status = preflight.build_preflight_status(run_tests=True)
    assert status["ok"]
    assert observed == [3600]


def test_custom_full_test_budget_reaches_native_runner(monkeypatch, tmp_path):
    stub_supporting_checks(monkeypatch, tmp_path)
    observed = []
    def run(cmd, *, timeout=120):
        if cmd[1:3] == ["-m", "pytest"]:
            observed.append(timeout)
        return {"cmd": cmd, "returncode": 0}
    monkeypatch.setattr(preflight, "_run", run)
    assert preflight.build_preflight_status(run_tests=True, pytest_timeout_seconds=2700)["ok"]
    assert observed == [2700]


@pytest.mark.parametrize("invalid", [None, True, 0, -1, 1.5, 86401])
def test_invalid_budget_rejected_before_any_report_writes(monkeypatch, tmp_path, invalid):
    root = tmp_path / "never-created"
    monkeypatch.setattr(preflight, "ROOT", root)
    with pytest.raises(ValueError):
        preflight.build_preflight_status(run_tests=True, pytest_timeout_seconds=invalid)
    assert not root.exists()


def test_real_child_timeout_returns_failed_check(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    result = preflight._run([sys.executable, "-c", "import time; time.sleep(3)"], timeout=.1)
    assert result["returncode"] == 124 and result["timed_out"] is True


@pytest.mark.parametrize("output,error", [(None, None), ("short", "problem"), (b"bytes", b"bad")])
def test_timeout_output_handles_none_text_and_bytes(monkeypatch, output, error):
    def expires(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output=output, stderr=error)
    monkeypatch.setattr(preflight.subprocess, "run", expires)
    result = preflight._run(["synthetic-command"])
    assert isinstance(result["stdout_tail"], str) and isinstance(result["stderr_tail"], str)
    assert result["returncode"] == 124


def test_launch_error_is_a_failed_check_without_exception_details(monkeypatch):
    def fails(*args, **kwargs):
        raise FileNotFoundError("synthetic-sensitive-detail")
    monkeypatch.setattr(preflight.subprocess, "run", fails)
    result = preflight._run(["synthetic-command"])
    assert result["returncode"] == 127
    assert result["launch_error"] == "FileNotFoundError"
    assert "synthetic-sensitive-detail" not in json.dumps(result)


def test_failed_or_timed_out_pytest_cannot_make_overall_preflight_green(monkeypatch, tmp_path):
    stub_supporting_checks(monkeypatch, tmp_path)
    def run(cmd, *, timeout=120):
        return {"cmd": cmd, "returncode": 124 if cmd[1:3] == ["-m", "pytest"] else 0,
            "timed_out": cmd[1:3] == ["-m", "pytest"]}
    monkeypatch.setattr(preflight, "_run", run)
    status = preflight.build_preflight_status(run_tests=True)
    assert not status["ok"] and status["checks"]["pytest"]["returncode"] == 124


def test_cli_passes_explicit_budget_and_writes_actual_build_result(monkeypatch, tmp_path, capsys):
    observed = {}
    def build(**kwargs):
        observed.update(kwargs)
        return {"ok": False, "checks": {"pytest": {"returncode": 124}}}
    monkeypatch.setattr(preflight, "build_preflight_status", build)
    monkeypatch.setattr(preflight, "STATUS_PATH", tmp_path / "report.json")
    monkeypatch.setattr(sys, "argv", ["preflight", "--run-tests", "--pytest-timeout-seconds", "1900"])
    assert preflight.main() == 1
    assert observed["pytest_timeout_seconds"] == 1900 and observed["run_tests"] is True
    assert json.loads((tmp_path / "report.json").read_text())["ok"] is False
    assert json.loads(capsys.readouterr().out)["checks"]["pytest"]["returncode"] == 124


@pytest.mark.parametrize("value", ["0", "-1", "86401", "1.5", "invalid"])
def test_cli_invalid_budget_never_runs_checks(monkeypatch, tmp_path, value):
    def unexpected(**kwargs):
        raise AssertionError("CLI validation must happen first")
    monkeypatch.setattr(preflight, "build_preflight_status", unexpected)
    monkeypatch.setattr(preflight, "STATUS_PATH", tmp_path / "report.json")
    monkeypatch.setattr(sys, "argv", ["preflight", "--pytest-timeout-seconds", value])
    with pytest.raises(SystemExit) as error:
        preflight.main()
    assert error.value.code == 2 and not (tmp_path / "report.json").exists()
