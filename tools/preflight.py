from __future__ import annotations

import argparse
import json
import py_compile
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
]


def _run(cmd: list[str], *, timeout: int = 120) -> dict[str, object]:
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    return {"cmd": cmd, "returncode": proc.returncode, "stdout_tail": proc.stdout[-4000:], "stderr_tail": proc.stderr[-4000:]}


def build_preflight_status(*, run_tests: bool = False) -> dict[str, object]:
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    interpreter = str(py if py.exists() else Path(sys.executable))
    compile_errors = []
    for rel in CRITICAL_MODULES:
        try:
            py_compile.compile(str(ROOT / rel), doraise=True)
        except Exception as exc:
            compile_errors.append({"path": rel, "error": str(exc)})
    checks = {
        "python": _run([interpreter, "-c", "import sys, numpy; print(sys.executable); print(numpy.__version__)"]),
        "config_import": _run([interpreter, "-c", "from config.config import CFG; print(CFG.DRY_RUN)"]),
        "env_example_exists": (ROOT / ".env.example").exists(),
        "profiles_dir_exists": (ROOT / "config" / "profiles").exists(),
        "compile_errors": compile_errors,
    }
    if run_tests:
        checks["pytest"] = _run([interpreter, "-m", "pytest", "-q"], timeout=240)
    status = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(ROOT),
        "interpreter": interpreter,
        "ok": not compile_errors and all(
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
