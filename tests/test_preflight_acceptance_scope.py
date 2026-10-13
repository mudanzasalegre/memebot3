"""Preflight evidence is scoped; caller assertions are not native test runs."""
import json
import sys

import pytest

from tools import preflight


def supporting(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    monkeypatch.setattr(preflight, "CRITICAL_MODULES", [])
    monkeypatch.setattr(preflight, "PAPER_HOTFIX_PROFILE", tmp_path / "profile.env")
    monkeypatch.setattr(preflight, "validate_paper_hotfix_profile", lambda path: {"ok": True})
    monkeypatch.setattr(preflight, "_report_dry_run_checks", lambda: [{"ok": True}])
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return {"cmd": command, "returncode": 0}

    monkeypatch.setattr(preflight, "_run", run)
    return seen


def test_external_pytest_claim_is_not_fabricated_native_exit_code(monkeypatch, tmp_path):
    seen = supporting(monkeypatch, tmp_path)
    status = preflight.build_preflight_status(external_pytest_passed=True)
    check = status["checks"]["pytest"]
    assert not any(command[1:3] == ["-m", "pytest"] for command in seen)
    assert "returncode" not in check and "cmd" not in check
    assert check["executed"] is False and check["caller_reported_passed"] is True
    assert status["validation_scope"]["pytest"] == "caller_reported_passed"


def test_supporting_checks_are_not_operational_or_financial_acceptance(monkeypatch, tmp_path):
    supporting(monkeypatch, tmp_path)
    status = preflight.build_preflight_status()
    assert status["ok"] is True
    scope = status["validation_scope"]
    assert scope["pytest"] == "not_run"
    assert scope["runtime"] == scope["provider_access"] == "not_tested"
    assert scope["profitability"] == "not_established"
    assert scope["whole_goal_complete"] is False


def test_cli_explicit_status_path_preserves_operator_status(monkeypatch, tmp_path, capsys):
    operator = tmp_path / "operator.json"
    operator.write_text('{"original":true}', encoding="utf-8")
    destination = tmp_path / "audit" / "native.json"
    monkeypatch.setattr(preflight, "STATUS_PATH", operator)
    monkeypatch.setattr(preflight, "build_preflight_status", lambda **kwargs: {"ok": True, "checks": {}})
    monkeypatch.setattr(sys, "argv", ["preflight", "--status-path", str(destination)])
    assert preflight.main() == 0
    assert json.loads(destination.read_text())["ok"] is True
    assert operator.read_text() == '{"original":true}'
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_new_runtime_and_training_dependencies_are_critical_compile_inputs():
    assert {"ml/train.py", "ml/retrain.py", "ml/training_daemon.py", "ml/training_lock.py",
            "ml/runner_advisory_learning.py", "runtime/owned_thread.py", "runtime/hot_queue.py",
            "runtime/loop_scheduler.py"}.issubset(set(preflight.CRITICAL_MODULES))


@pytest.mark.parametrize("returncode,expected", [(0, "passed"), (1, "failed"), (5, "failed"), (124, "failed")])
def test_executed_pytest_scope_uses_actual_result_not_external_claim(monkeypatch, tmp_path, returncode, expected):
    seen = supporting(monkeypatch, tmp_path)

    def run(command, **kwargs):
        seen.append(command)
        return {"cmd": command, "returncode": returncode if command[1:3] == ["-m", "pytest"] else 0}

    monkeypatch.setattr(preflight, "_run", run)
    status = preflight.build_preflight_status(run_tests=True, external_pytest_passed=True)
    assert status["validation_scope"]["pytest"] == expected
    assert status["checks"]["pytest"]["executed"] is True
    assert status["checks"]["pytest"]["source"] == "native_preflight"
    assert status["ok"] is (returncode == 0)
    assert sum(command[1:3] == ["-m", "pytest"] for command in seen) == 1


@pytest.mark.parametrize("filename", [".env", "model.pkl", "not-json.txt"])
def test_non_json_status_path_is_rejected_before_checks(monkeypatch, tmp_path, filename):
    destination = tmp_path / filename
    destination.write_text("original", encoding="utf-8")
    monkeypatch.setattr(preflight, "build_preflight_status", lambda **kwargs: pytest.fail("invalid path must not run checks"))
    monkeypatch.setattr(sys, "argv", ["preflight", "--status-path", str(destination)])
    with pytest.raises(SystemExit) as error:
        preflight.main()
    assert error.value.code == 2 and destination.read_text() == "original"


def test_failed_launch_is_not_reported_as_an_executed_pytest(monkeypatch, tmp_path):
    supporting(monkeypatch, tmp_path)

    def run(command, **kwargs):
        if command[1:3] == ["-m", "pytest"]:
            return {"cmd": command, "returncode": 127, "launch_error": "FileNotFoundError"}
        return {"cmd": command, "returncode": 0}

    monkeypatch.setattr(preflight, "_run", run)
    status = preflight.build_preflight_status(run_tests=True)
    assert status["validation_scope"]["pytest"] == "launch_failed"
    assert status["checks"]["pytest"]["executed"] is False
    assert status["ok"] is False
