from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from api.services.live_promotion import build_live_start_profile_values
from runtime.live_canary_guard import (
    evaluate_live_start,
    load_env_values,
    validate_live_canary_profile_values,
    validate_paper_sample,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_live_artifacts(root: Path) -> None:
    _write_json(
        root / "data" / "metrics" / "current_run_summary.json",
        {"closed_trades": 30, "buys": 30, "total_pnl_usd": 8.5, "profit_factor": 1.15},
    )
    _write_json(
        root / "data" / "research_runs" / "scoreboard.json",
        {
            "entries": [
                {
                    "run_id": "ar_live_ok",
                    "status": "accepted_replay",
                    "objective_score": 4.2,
                    "evaluated_at_utc": "2026-07-07T14:00:00+00:00",
                }
            ]
        },
    )


def test_live_guard_blocks_checked_in_template_without_manual_approval() -> None:
    values = load_env_values(Path("config/profiles/live_canary_safe.env"))

    result = validate_live_canary_profile_values(values, label="live_canary_safe.env", require_approval=True)

    assert not result.passed
    blocked = [gate.id for gate in result.gates if not gate.passed]
    assert "manual.profile_not_template" in blocked
    assert "manual.approval_flag" in blocked


def test_live_guard_accepts_generated_manual_profile_with_artifacts(tmp_path: Path) -> None:
    _write_live_artifacts(tmp_path)
    values = build_live_start_profile_values(approved_by="operator", approval_id="approval-1")

    result = evaluate_live_start(
        root=tmp_path,
        cfg=SimpleNamespace(
            LIVE_PROMOTION_MIN_PAPER_CLOSED_TRADES=25,
            LIVE_PROMOTION_MIN_NET_PNL_USD=0.0,
            LIVE_PROMOTION_MIN_PROFIT_FACTOR=1.0,
        ),
        profile_values=values,
        profile_label="generated",
    )

    assert result.passed, result.errors


def test_live_guard_blocks_negative_0707_sample(tmp_path: Path) -> None:
    _write_live_artifacts(tmp_path)
    _write_json(
        tmp_path / "data" / "metrics" / "current_run_summary.json",
        {"closed_trades": 30, "buys": 30, "total_pnl_usd": -81.33, "profit_factor": 0.629},
    )
    values = build_live_start_profile_values(approved_by="operator", approval_id="approval-1")

    result = evaluate_live_start(root=tmp_path, cfg=SimpleNamespace(), profile_values=values, profile_label="generated")

    assert not result.passed
    blocked = [gate.id for gate in result.gates if not gate.passed]
    assert "sample.net_pnl" in blocked
    assert "sample.profit_factor" in blocked


def test_live_guard_requires_minimum_paper_sample() -> None:
    result = validate_paper_sample({"closed_trades": 24, "total_pnl_usd": 5.0, "profit_factor": 1.2})

    assert not result.passed
    assert "sample.closed_trades" in [gate.id for gate in result.gates if not gate.passed]


def test_live_guard_rejects_stale_manual_approval() -> None:
    values = build_live_start_profile_values(approved_by="operator", approval_id="approval-1")
    values["LIVE_CANARY_APPROVED_AT_UTC"] = "2026-07-06T00:00:00+00:00"

    result = validate_live_canary_profile_values(
        values,
        label="generated",
        require_approval=True,
    )

    assert not result.passed
    assert "manual.approval_freshness" in [gate.id for gate in result.gates if not gate.passed]
