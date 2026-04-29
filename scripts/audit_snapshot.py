from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.venv_bootstrap import ensure_project_venv

ensure_project_venv(__file__, module_name=__spec__.name if __spec__ else None)

from analytics.audit import _json_value, build_audit_snapshot, render_audit_markdown, write_normalized_candidate_outcomes  # noqa: E402
from api.settings import get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera snapshot integral de auditoria y artefactos derivados seguros.")
    parser.add_argument("--write-json", default="data/metrics/audit_snapshot.json", help="Ruta del snapshot JSON.")
    parser.add_argument("--write-markdown", default="docs/AUDIT_REPORT.md", help="Ruta del markdown de auditoria.")
    parser.add_argument(
        "--write-normalized-jsonl",
        default="data/metrics/candidate_outcomes.normalized.jsonl",
        help="Ruta del JSONL normalizado derivado.",
    )
    args = parser.parse_args()

    settings = get_settings()
    snapshot = build_audit_snapshot(
        db_path=settings.db_path,
        features_dir=settings.features_dir,
        runtime_events_path=settings.runtime_events_path,
        research_events_path=settings.research_events_path,
        paper_portfolio_path=settings.paper_portfolio_path,
        research_portfolio_path=settings.data_dir / "research_portfolio.json",
        research_scorecard_path=settings.research_scorecard_json,
        research_thresholds_path=settings.research_thresholds_json,
        recommended_threshold_path=settings.recommended_threshold_json,
        train_status_path=settings.train_status_json,
        dataset_quality_path=settings.dataset_quality_json,
        logs_dir=settings.logs_dir,
        edge_report_path=settings.project_root / "docs" / "EDGE_REPORT.md",
        ml_report_path=settings.project_root / "docs" / "ML_REPORT.md",
    )

    normalized_result = write_normalized_candidate_outcomes(
        events_path=settings.research_events_path,
        output_path=Path(args.write_normalized_jsonl),
    )
    snapshot["research"]["normalized_candidate_events"]["derived_output_path"] = normalized_result["path"]

    markdown = render_audit_markdown(snapshot)
    print(markdown.encode("ascii", errors="replace").decode("ascii"))

    json_target = Path(str(args.write_json or "").strip())
    if str(json_target):
        json_target.parent.mkdir(parents=True, exist_ok=True)
        json_target.write_text(json.dumps(_json_value(snapshot), indent=2, ensure_ascii=False), encoding="utf-8")

    md_target = Path(str(args.write_markdown or "").strip())
    if str(md_target):
        md_target.parent.mkdir(parents=True, exist_ok=True)
        md_target.write_text(markdown, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
