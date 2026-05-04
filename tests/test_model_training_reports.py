from __future__ import annotations

from pathlib import Path

from ml.model_validation_warnings import collect_report_warnings, target_validation_payload
from tools.train_model_reports import build_model_training_bundle, write_model_training_bundle


def _fake_report(name: str) -> dict:
    return {
        "family": name,
        "status": "ok",
        "rows": 10,
        "targets": {
            "target": {
                "status": "trained",
                "validation": target_validation_payload(
                    warnings=["in_sample_only", "not_enough_rows", "not_ready_for_enforcement"]
                ),
            }
        },
    }


def test_collect_report_warnings_finds_nested_critical_warnings() -> None:
    validation = collect_report_warnings(_fake_report("risk"))
    assert validation["has_critical_warnings"] is True
    assert "not_ready_for_enforcement" in validation["critical_warnings"]


def test_model_training_bundle_never_promotes_or_enforces() -> None:
    jobs = (("risk", lambda: _fake_report("risk"), "risk_model_report.json"),)
    bundle = build_model_training_bundle(jobs=jobs, root=Path("."))
    assert bundle["promotion"]["attempted"] is False
    assert bundle["enforcement"]["enabled"] is False
    assert bundle["validation"]["ready_for_enforcement"] is False
    assert "not_enough_rows" in bundle["validation"]["critical_warnings"]


def test_write_model_training_bundle_outputs_docs_and_json(tmp_path) -> None:
    jobs = (("risk", lambda: _fake_report("risk"), "risk_model_report.json"),)
    bundle = write_model_training_bundle(tmp_path, jobs=jobs)
    assert bundle["reports"]["risk"]["status"] == "ok"
    assert (tmp_path / "data" / "metrics" / "model_training_report.json").exists()
    assert (tmp_path / "docs" / "MODEL_TRAINING_REPORT.md").exists()
