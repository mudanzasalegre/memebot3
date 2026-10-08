from __future__ import annotations

import argparse
import json
import math
import os
import py_compile
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from runtime.live_canary_guard import live_flags_declared, validate_live_canary_profile_values

STATUS_PATH = ROOT / "data" / "metrics" / "preflight_status.json"
PAPER_HOTFIX_PROFILE = ROOT / "config" / "profiles" / "paper_hotfix_0707.env"
LIVE_CANARY_TEMPLATE_NAMES = {"live_canary_safe.env", "sniper_live_canary.env"}

TRUE_TEXT = {"1", "true", "yes", "y", "on"}
FALSE_TEXT = {"0", "false", "no", "n", "off"}
SECRET_HINTS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PRIVATE",
    "PASSWORD",
    "RPC_URL",
    "WALLET",
    "HELIUS",
    "BIRDEYE",
    "RUGCHECK",
    "AUTH",
)

PAPER_HOTFIX_REQUIRED_TRUE = (
    "DRY_RUN",
    "STRATEGY_OPTIMIZATION_LOCK",
    "LANE_SIZING_ENABLED",
    "PAPER_BOOTSTRAP_QUALITY_GATES_ENABLED",
    "PAPER_BOOTSTRAP_REQUIRE_ROUTE",
    "PAPER_BOOTSTRAP_REQUIRE_PUMPSWAP",
    "PAPER_BOOTSTRAP_REQUIRE_REAL_LIQUIDITY",
    "PAPER_BOOTSTRAP_REQUIRE_EXACT_AMOUNT",
    "PAPER_BOOTSTRAP_BLOCK_CLUSTER_BAD",
    "PAPER_BOOTSTRAP_ENABLED",
    "ML_RISK_SHADOW_ONLY",
    "CURRENT_RUN_AUTOTUNE_ENABLED",
)

PAPER_HOTFIX_REQUIRED_FALSE = (
    "LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED",
    "PAPER_EXPLORATION_QUOTA_ENABLED",
    "PAPER_IDLE_MICRO_EXPLORATION_ENABLED",
    "SHADOW_FOLLOWUP_MICRO_ENABLED",
    "SHADOW_FOLLOWUP_REAL_LIQUIDITY_BREAKOUT_ENABLED",
    "MOONSHOT_MICRO_LOTTERY_ENABLED",
    "BIRTH_PROBE_MICRO_CANARY_ENABLED",
    "SNIPER_RESEARCH_MICRO_FALLBACK_ENABLED",
    "RESEARCH_RANK_CANARY_PAPER_ENABLED",
    "RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED",
    "RESEARCH_RANK_CANARY_PAPER_NORMAL_BUY_ENABLED",
    "LATE_MOMENTUM_WATCH_BUY_ENABLED",
    "LATE_MOMENTUM_WATCH_PAPER_CANARY_ENABLED",
    "PAPER_AGGRESSIVE_TRADING_ENABLED",
    "GREEN_SNIPER_BUY_RESTRICTED_ENABLED",
    "PUMPFUN_STANDARD_BUY_ENABLED",
    "DEX_MATURE_STANDARD_BUY_ENABLED",
    "ALLOW_UNTAGGED_STANDARD_BUY",
    "LIVE_CANARY_ENABLED",
    "GREEN_SNIPER_LIVE_ENABLED",
    "RESEARCH_RANK_CANARY_LIVE_ENABLED",
    "LATE_MOMENTUM_WATCH_LIVE_ENABLED",
    "LIVE_AGGRESSIVE_TRADING_ENABLED",
    "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED",
    "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED",
    "SNIPER_RESEARCH_MICRO_FALLBACK_LIVE_ENABLED",
    "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED",
    "POST_PARTIAL_PROTECTION_LIVE_ENABLED",
    "BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED",
    "RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED",
    "AUTO_PROMOTE_LIVE",
    "MODEL_AUTO_PROMOTE",
    "ML_AUTO_PROMOTE_LANES",
    "ML_ALLOW_RESEARCH_LIVE",
    "ML_ALLOW_UNKNOWN_LIVE",
    "ALLOW_LIVE_POLICY_ENFORCE",
    "LLM_TRADING_ENABLED",
    "AUTORESEARCH_AUTO_PAPER_PROMOTE",
    "AUTORESEARCH_AUTO_LIVE_PROMOTE",
    "AUTORESEARCH_LIVE_PROMOTION_ENABLED",
    "AUTORESEARCH_LLM_CAN_TOUCH_LIVE",
    "ML_RISK_MODEL_ENABLED",
    "ML_RISK_VETO_ENABLED",
    "ML_EV_MODEL_ENABLED",
    "ML_SIZING_ENABLED",
    "ML_SHADOW_CANDIDATE_MODEL_FALLBACK_ENABLED",
    "CURRENT_RUN_AUTOTUNE_APPLY_ENABLED",
)

PAPER_HOTFIX_FINITE_CAPS = (
    "SHADOW_FOLLOWUP_MICRO_MAX_OPEN",
    "SHADOW_FOLLOWUP_MICRO_MAX_DAILY_BUYS",
    "PAPER_BOOTSTRAP_MAX_OPEN",
    "PAPER_BOOTSTRAP_MAX_DAILY_BUYS",
    "PAPER_BOOTSTRAP_MAX_HOURLY_BUYS",
    "PAPER_EXPLORATION_MAX_OPEN",
    "PAPER_IDLE_MAX_DAILY_BUYS",
    "MOONSHOT_MICRO_LOTTERY_MAX_OPEN",
    "MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS",
)

PAPER_HOTFIX_MAX_AMOUNTS_SOL = {
    "PAPER_MAX_TRADE_AMOUNT_SOL": 0.1,
    "PAPER_BOOTSTRAP_AMOUNT_SOL": 0.1,
    "PAPER_BOOTSTRAP_MAX_AMOUNT_SOL": 0.1,
    "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL": 0.003,
    "MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL": 0.001,
}

PAPER_HOTFIX_EXACT_AMOUNTS_SOL = {
    "PAPER_MAX_TRADE_AMOUNT_SOL": 0.1,
    "PAPER_BOOTSTRAP_AMOUNT_SOL": 0.1,
    "PAPER_BOOTSTRAP_MAX_AMOUNT_SOL": 0.1,
}

CRITICAL_MODULES = [
    "run_bot.py",
    "trader/papertrading.py",
    "analytics/forward_evidence.py",
    "analytics/paper_forward.py",
    "analytics/current_run.py",
    "analytics/paper_bootstrap.py",
    "analytics/funnel_attribution.py",
    "analytics/baseline_snapshot.py",
    "analytics/decision_ledger.py",
    "backtest/event_replay.py",
    "backtest/policy_replay.py",
    "execution/trade_decision.py",
    "execution/jupiter_managed_contract.py",
    "execution/chain_reconciliation.py",
    "execution/wallet_effects.py",
    "execution/unsigned_projection.py",
    "execution/authority_observations.py",
    "runtime/execution_provenance.py",
    "runtime/buy_recovery.py",
    "runtime/sell_recovery.py",
    "runtime/close_recovery.py",
    "utils/solana_execution.py",
    "utils/jupiter_access.py",
    "fetcher/jupiter_router.py",
    "fetcher/jupiter_price.py",
    "trader/buyer.py",
    "trader/seller.py",
    "features/decision_store.py",
    "ml/label_builder.py",
    "ml/feature_sets.py",
    "runtime/entry_policy.py",
    "runtime/dynamic_thresholds.py",
    "runtime/live_canary_guard.py",
    "runtime/live_canary_v2.py",
    "runtime/position_limits.py",
    "tools/config_effect_audit.py",
]

REPORT_BUILDERS = [
    ("baseline", "analytics.baseline_snapshot", "build_current_baseline_snapshot"),
    ("funnel", "analytics.funnel_attribution", "build_funnel_attribution"),
    ("missed_pumps", "analytics.missed_pumps", "build_missed_pumps"),
    ("event_replay", "backtest.event_replay", "build_event_replay"),
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


def _truthy_text(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_TEXT


def _falsey_text(value: str | None) -> bool:
    return str(value or "").strip().lower() in FALSE_TEXT


def _float_value(value: str | None) -> float | None:
    try:
        if value is None or not str(value).strip():
            return None
        return float(str(value).strip())
    except Exception:
        return None


def _resolve_repo_path(raw: str | None, default: Path) -> Path:
    value = str(raw or "").strip()
    path = Path(value) if value else default
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _feature_dataset_status() -> dict[str, object]:
    features_dir = _resolve_repo_path(os.getenv("FEATURES_DIR"), ROOT / "data" / "features")
    files = sorted(features_dir.glob("features_*.parquet")) or sorted(features_dir.glob("features_*.csv"))
    latest = max(files, key=lambda path: path.stat().st_mtime) if files else None
    latest_mtime = datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc).isoformat() if latest else None
    return {
        "features_dir": str(features_dir),
        "exists": features_dir.exists(),
        "file_count": len(files),
        "latest_file": str(latest) if latest else None,
        "latest_mtime_utc": latest_mtime,
        "usable": bool(files),
    }


def _is_secret_key(key: str) -> bool:
    upper = str(key).upper()
    return any(hint in upper for hint in SECRET_HINTS)


def redact_env_values(values: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in sorted(values.items()):
        redacted[key] = "<redacted>" if _is_secret_key(key) and str(value).strip() else str(value)
    return redacted


def validate_paper_hotfix_values(values: dict[str, str], *, label: str = "paper_hotfix_0707") -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []

    if not values:
        errors.append(f"{label} profile is missing or empty")

    for name in PAPER_HOTFIX_REQUIRED_TRUE:
        if name not in values:
            errors.append(f"{label} missing required key: {name}")
        elif not _truthy_text(values.get(name)):
            errors.append(f"{label} requires {name}=true")

    for name in PAPER_HOTFIX_REQUIRED_FALSE:
        if name not in values:
            errors.append(f"{label} missing required key: {name}")
        elif not _falsey_text(values.get(name)):
            errors.append(f"{label} requires {name}=false")

    for name in PAPER_HOTFIX_FINITE_CAPS:
        numeric = _float_value(values.get(name))
        if numeric is None:
            errors.append(f"{label} requires numeric finite cap: {name}")
        elif numeric <= 0:
            errors.append(f"{label} requires {name}>0 because 0 means unlimited")

    for name, maximum in PAPER_HOTFIX_MAX_AMOUNTS_SOL.items():
        numeric = _float_value(values.get(name))
        if numeric is None:
            errors.append(f"{label} requires numeric amount: {name}")
        elif numeric <= 0:
            errors.append(f"{label} requires {name}>0")
        elif numeric > maximum:
            errors.append(f"{label} requires {name}<={maximum:g}")

    for name, expected in PAPER_HOTFIX_EXACT_AMOUNTS_SOL.items():
        numeric = _float_value(values.get(name))
        if numeric is None or abs(numeric - expected) > 1e-12:
            errors.append(f"{label} requires {name}={expected:g}")
    for name, default, upper in (("PAPER_FILL_SLIPPAGE_BPS", "100", 10000),
                                  ("PAPER_FILL_FEE_SOL", "0.000025", 0.1)):
        numeric = _float_value(values.get(name, default))
        if numeric is None or not math.isfinite(numeric) or numeric < 0 or numeric >= upper:
            errors.append(f"{label} invalid paper cost assumption: {name}")

    profile_secret_keys = sorted(key for key, value in values.items() if _is_secret_key(key) and str(value).strip())
    if profile_secret_keys:
        errors.append(f"{label} must not contain secrets: {','.join(profile_secret_keys)}")

    return {
        "ok": not errors,
        "path": str(PAPER_HOTFIX_PROFILE.relative_to(ROOT)) if PAPER_HOTFIX_PROFILE.is_absolute() else str(PAPER_HOTFIX_PROFILE),
        "vars": len(values),
        "errors": errors,
        "warnings": warnings,
        "redacted_values": redact_env_values(values),
    }


def validate_paper_hotfix_profile(path: Path = PAPER_HOTFIX_PROFILE) -> dict[str, object]:
    values = _load_env_file(path)
    label = str(path.relative_to(ROOT)) if path.is_absolute() and path.is_relative_to(ROOT) else path.name
    result = validate_paper_hotfix_values(values, label=label)
    result["path"] = label
    return result


def validate_live_canary_template_profile(path: Path) -> dict[str, object]:
    values = _load_env_file(path)
    label = str(path.relative_to(ROOT)) if path.is_absolute() and path.is_relative_to(ROOT) else path.name
    errors: list[str] = []
    warnings: list[str] = []
    if not live_flags_declared(values):
        return {
            "ok": True,
            "path": label,
            "vars": len(values),
            "errors": errors,
            "warnings": warnings,
            "redacted_values": redact_env_values(values),
        }
    if path.name not in LIVE_CANARY_TEMPLATE_NAMES:
        errors.append(f"{label} declares live flags but is not an approved live canary template")
    if not _truthy_text(values.get("LIVE_CANARY_PROFILE_TEMPLATE")):
        errors.append(f"{label} requires LIVE_CANARY_PROFILE_TEMPLATE=true")
    guard_result = validate_live_canary_profile_values(values, label=label, require_approval=False)
    errors.extend(guard_result.errors)
    profile_secret_keys = sorted(key for key, value in values.items() if _is_secret_key(key) and str(value).strip())
    if profile_secret_keys:
        errors.append(f"{label} must not contain secrets: {','.join(profile_secret_keys)}")
    return {
        "ok": not errors,
        "path": label,
        "vars": len(values),
        "errors": errors,
        "warnings": warnings,
        "redacted_values": redact_env_values(values),
    }


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


def build_preflight_status(*, run_tests: bool = False, external_pytest_passed: bool = False) -> dict[str, object]:
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    interpreter = str(py if py.exists() else Path(sys.executable))
    base_dir_paths = (ROOT / "data" / "metrics", ROOT / "docs")
    for path in base_dir_paths:
        path.mkdir(parents=True, exist_ok=True)
    compile_errors = []
    for rel in CRITICAL_MODULES:
        try:
            py_compile.compile(str(ROOT / rel), doraise=True)
        except Exception as exc:
            compile_errors.append({"path": rel, "error": str(exc)})
    env_example = _load_env_file(ROOT / ".env.example")
    profile_files = sorted((ROOT / "config" / "profiles").glob("*.env"))
    profiles = {str(path.relative_to(ROOT)): {"vars": len(_load_env_file(path))} for path in profile_files}
    paper_hotfix_profile = validate_paper_hotfix_profile(PAPER_HOTFIX_PROFILE)
    live_canary_profiles = {
        str(path.relative_to(ROOT)): validate_live_canary_template_profile(path)
        for path in profile_files
        if live_flags_declared(_load_env_file(path))
    }
    base_dirs = {str(path.relative_to(ROOT)): path.exists() for path in base_dir_paths}
    report_checks = _report_dry_run_checks()
    checks = {
        "python": _run([interpreter, "-c", "import sys, numpy; print(sys.executable); print(numpy.__version__)"]),
        "config_import": _run([interpreter, "-c", "from config.config import CFG; print(CFG.DRY_RUN)"]),
        "env_example": {"exists": (ROOT / ".env.example").exists(), "vars": len(env_example)},
        "profiles_dir_exists": (ROOT / "config" / "profiles").exists(),
        "profiles": profiles,
        "paper_hotfix_0707_profile": paper_hotfix_profile,
        "live_canary_template_profiles": live_canary_profiles,
        "base_dirs": base_dirs,
        "report_builders_no_data": report_checks,
        "feature_dataset": _feature_dataset_status(),
        "model_optional": not (ROOT / "ml" / "models" / "active_model.pkl").exists(),
        "compile_errors": compile_errors,
    }
    if run_tests:
        checks["pytest"] = _run([interpreter, "-m", "pytest", "-q"], timeout=240)
    elif external_pytest_passed:
        checks["pytest"] = {
            "cmd": [interpreter, "-m", "pytest", "-q"],
            "returncode": 0,
            "stdout_tail": "pytest already passed in startup preflight",
            "stderr_tail": "",
        }
    status = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(ROOT),
        "interpreter": interpreter,
        "ok": not compile_errors
        and bool(paper_hotfix_profile.get("ok"))
        and all(bool(item.get("ok")) for item in live_canary_profiles.values())
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
    parser.add_argument(
        "--external-pytest-passed",
        action="store_true",
        help="record pytest as passed because the caller already ran it successfully",
    )
    args = parser.parse_args()
    status = build_preflight_status(
        run_tests=args.run_tests,
        external_pytest_passed=args.external_pytest_passed,
    )
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
    print(json.dumps(status, indent=2, default=str))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
