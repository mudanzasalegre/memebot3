from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from api.services.live_promotion import build_live_promotion_preflight, write_live_start_profile


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _settings(tmp_path: Path):
    return SimpleNamespace(
        data_dir=tmp_path / "data",
        metrics_dir=tmp_path / "data" / "metrics",
        runtime_dir=tmp_path / "data" / "runtime",
    )


def _healthy_artifacts(root: Path) -> None:
    _write_json(root / "data" / "metrics" / "current_run_summary.json", {"buys": 30, "closed_trades": 30})
    _write_json(
        root / "data" / "research_runs" / "scoreboard.json",
        [
            {
                "run_id": "ar_ok",
                "status": "accepted_replay",
                "objective_score": 12.5,
                "evaluated_at_utc": "2026-06-04T20:00:00+00:00",
            }
        ],
    )
    _write_json(
        root / "data" / "research_runs" / "api_budget.json",
        {"birdeye_429_count": 0, "gecko_429_count": 0, "jupiter_rate_limit_count": 0, "provider_degraded_minutes": 0},
    )


def test_live_promotion_preflight_blocks_api_budget(tmp_path) -> None:
    _healthy_artifacts(tmp_path)
    _write_json(
        tmp_path / "data" / "research_runs" / "api_budget.json",
        {"birdeye_429_count": 4, "gecko_429_count": 1, "jupiter_rate_limit_count": 256, "provider_degraded_minutes": 0},
    )

    preflight = build_live_promotion_preflight(
        _settings(tmp_path),
        runtime_snapshot={"wallet_sol": 1.0, "dry_run": True},
    )

    assert not preflight["passed"]
    blocked = [gate["id"] for gate in preflight["gates"] if gate["status"] != "pass"]
    assert "api_budget" in blocked


def test_live_promotion_preflight_writes_live_profile_after_gates_pass(tmp_path) -> None:
    _healthy_artifacts(tmp_path)
    settings = _settings(tmp_path)

    preflight = build_live_promotion_preflight(settings, runtime_snapshot={"wallet_sol": 1.0, "dry_run": True})
    profile_path = write_live_start_profile(settings, preflight)

    assert preflight["passed"]
    text = profile_path.read_text(encoding="utf-8")
    assert "DRY_RUN=0" in text
    assert "STRATEGY_OPTIMIZATION_LOCK=false" in text
    assert "ALLOW_UNTAGGED_STANDARD_BUY=false" in text
