from __future__ import annotations

import argparse
import json
import py_compile
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATUS_PATH = ROOT / "data" / "metrics" / "preflight_status.json"

CRITICAL_MODULES = [
    "run_bot.py",
    "analytics/funnel_attribution.py",
    "analytics/baseline_snapshot.py",
    "analytics/decision_ledger.py",
    "backtest/policy_replay.py",
    "execution/trade_decision.py",
    "features/decision_store.py",
    "ml/label_builder.py",
    "ml/feature_sets.py",
    "runtime/entry_policy.py",
    "runtime/dynamic_thresholds.py",
    "runtime/live_canary_v2.py",
    "runtime/position_limits.py",
    "tools/config_effect_audit.py",
]

REPORT_BUILDERS = [
    ("baseline", "analytics.baseline_snapshot", "build_current_baseline_snapshot"),
    ("funnel", "analytics.funnel_attribution", "build_funnel_attribution"),
    ("missed_pumps", "analytics.missed_pumps", "build_missed_pumps"),
    ("policy_replay", "backtest.policy_replay", "build_policy_replay"),
    ("runner_capture", "analytics.runner_capture", "build_runner_capture"),
    ("trade_diagnostics", "analytics.trade_diagnostics", "build_trade_diagnostics"),
]


def _run(cmd: list[str], *, timeout: int = 120) -> dict[str, object]:
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    return {"cmd": cmd, "returncode": proc.returncode, "stdout_tail": proc.stdout[-4000:], "stderr_tail": proc.stderr[-4000:]}


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _report_dry_run_checks() -> list[dict[str, object]]:
    import importlib

    checks: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="memebot3_preflight_") as tmp:
        root = Path(tmp)
        (root / "data" / "metrics").mkdir(parents=True, exist_ok=True)
        (root / "docs").mkdir(parents=True, exist_ok=True)
        for name, module_name, func_name in REPORT_BUILDERS:
            try:
                module = importlib.import_module(module_name)
                payload = getattr(module, func_name)(root)
                checks.append({"name": name, "ok": True, "rows_or_keys": len(payload) if hasattr(payload, "__len__") else None})
            except Exception as exc:
                checks.append({"name": name, "ok": False, "error": repr(exc)})
    return checks


def build_preflight_status(*, run_tests: bool = False) -> dict[str, object]:
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    interpreter = str(py if py.exists() else Path(sys.executable))
    compile_errors = []
    for rel in CRITICAL_MODULES:
        try:
            py_compile.compile(str(ROOT / rel), doraise=True)
        except Exception as exc:
            compile_errors.append({"path": rel, "error": str(exc)})
    env_example = _load_env_file(ROOT / ".env.example")
    profile_files = sorted((ROOT / "config" / "profiles").glob("*.env"))
    profiles = {str(path.relative_to(ROOT)): {"vars": len(_load_env_file(path))} for path in profile_files}
    base_dirs = {str(path.relative_to(ROOT)): path.exists() for path in (ROOT / "data" / "metrics", ROOT / "docs")}
    report_checks = _report_dry_run_checks()
    checks = {
        "python": _run([interpreter, "-c", "import sys, numpy; print(sys.executable); print(numpy.__version__)"]),
        "config_import": _run([interpreter, "-c", "from config.config import CFG; print(CFG.DRY_RUN)"]),
        "env_example": {"exists": (ROOT / ".env.example").exists(), "vars": len(env_example)},
        "profiles_dir_exists": (ROOT / "config" / "profiles").exists(),
        "profiles": profiles,
        "base_dirs": base_dirs,
        "report_builders_no_data": report_checks,
        "model_optional": not (ROOT / "ml" / "models" / "active_model.pkl").exists(),
        "compile_errors": compile_errors,
    }
    if run_tests:
        checks["pytest"] = _run([interpreter, "-m", "pytest", "-q"], timeout=240)
    status = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(ROOT),
        "interpreter": interpreter,
        "ok": not compile_errors
        and all(base_dirs.values())
        and all(item.get("ok") for item in report_checks)
        and all(
            value.get("returncode", 0) == 0 for value in checks.values() if isinstance(value, dict) and "returncode" in value
        ),
        "checks": checks,
    }
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="Run MemeBot3 local preflight checks.")
    parser.add_argument("--run-tests", action="store_true", help="also run the full pytest suite")
    args = parser.parse_args()
    status = build_preflight_status(run_tests=args.run_tests)
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
    print(json.dumps(status, indent=2, default=str))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
