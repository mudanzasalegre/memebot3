from __future__ import annotations

import json

from analytics.core_report_scheduler import REQUIRED_CORE_REPORTS, regenerate_core_reports


def test_missing_reports_are_created_with_empty_data(tmp_path) -> None:
    summary = regenerate_core_reports(tmp_path)

    assert summary["reports"]
    for name in REQUIRED_CORE_REPORTS:
        path = tmp_path / "data" / "metrics" / name
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        assert payload.get("generated_at_utc")


def test_policy_replay_placeholder_safe_without_rows(tmp_path) -> None:
    summary = regenerate_core_reports(tmp_path)

    assert "policy_replay.json" in summary["reports"]
    payload = json.loads((tmp_path / "data" / "metrics" / "policy_replay.json").read_text(encoding="utf-8"))
    assert payload.get("generated_at_utc")
