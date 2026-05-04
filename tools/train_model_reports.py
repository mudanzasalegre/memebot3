from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.report_utils import write_json, write_markdown
from config.config import PROJECT_ROOT
from ml.model_validation_warnings import (
    WARNING_NOT_READY_FOR_ENFORCEMENT,
    collect_report_warnings,
    target_validation_payload,
)
from ml.train_continuation_model import train_continuation_models
from ml.train_ev_model import train_ev_models
from ml.train_risk_model import train_risk_models
from ml.train_runner_model import train_runner_models


TRAINING_JOBS: tuple[tuple[str, Callable[[], dict[str, Any]], str], ...] = (
    ("risk", train_risk_models, "risk_model_report.json"),
    ("ev", train_ev_models, "ev_model_report.json"),
    ("runner", train_runner_models, "runner_model_report.json"),
    ("continuation", train_continuation_models, "continuation_model_report.json"),
)


def _job_failure_report(name: str, exc: BaseException) -> dict[str, Any]:
    return {
        "family": name,
        "status": "failed",
        "error": str(exc),
        "traceback": traceback.format_exc(limit=8),
        "validation": target_validation_payload(warnings=[WARNING_NOT_READY_FOR_ENFORCEMENT]),
    }


def build_model_training_bundle(
    *,
    jobs: tuple[tuple[str, Callable[[], dict[str, Any]], str], ...] = TRAINING_JOBS,
    root: Path | None = None,
) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    reports: dict[str, Any] = {}
    for name, train_func, filename in jobs:
        try:
            report = train_func()
        except Exception as exc:
            report = _job_failure_report(name, exc)
            write_json(root / "data" / "metrics" / filename, report)
        reports[name] = report

    validations = {name: collect_report_warnings(report) for name, report in reports.items()}
    all_warnings = sorted({warning for validation in validations.values() for warning in validation["warnings"]})
    all_critical = sorted({warning for validation in validations.values() for warning in validation["critical_warnings"]})
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "reports": reports,
        "validation": {
            "warnings": all_warnings,
            "critical_warnings": all_critical,
            "has_critical_warnings": bool(all_critical),
            "ready_for_enforcement": False,
            "by_family": validations,
        },
        "promotion": {
            "attempted": False,
            "allowed": False,
            "reason": "manual_report_bundle_only",
        },
        "enforcement": {
            "enabled": False,
            "reason": "manual_report_bundle_only",
        },
    }


def write_model_training_bundle(
    root: Path | None = None,
    *,
    jobs: tuple[tuple[str, Callable[[], dict[str, Any]], str], ...] = TRAINING_JOBS,
) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    bundle = build_model_training_bundle(jobs=jobs, root=root)
    metrics = root / "data" / "metrics"
    write_json(metrics / "model_training_report.json", bundle)
    lines = [
        "# Model Training Report",
        "",
        f"- Generated at UTC: `{bundle['generated_at_utc']}`",
        "- Promotion attempted: `false`",
        "- Enforcement enabled: `false`",
        f"- Ready for enforcement: `{bundle['validation']['ready_for_enforcement']}`",
        f"- Critical warnings: `{', '.join(bundle['validation']['critical_warnings']) or 'none'}`",
        "",
        "| Family | Status | Rows | Warnings | Critical |",
        "|---|---|---:|---|---|",
    ]
    for family, report in bundle["reports"].items():
        validation = bundle["validation"]["by_family"][family]
        lines.append(
            f"| {family} | {report.get('status', 'unknown')} | {report.get('rows', 0)} | "
            f"{', '.join(validation['warnings']) or 'none'} | {', '.join(validation['critical_warnings']) or 'none'} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- These models are trained for reports only.",
            "- Validation is marked in-sample unless a future holdout path is added.",
            "- Critical warnings block enforcement in `strategy_quality_gate`.",
        ]
    )
    write_markdown(root / "docs" / "MODEL_TRAINING_REPORT.md", lines)
    return bundle


def main() -> int:
    print(json.dumps(write_model_training_bundle(), indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
