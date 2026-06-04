from __future__ import annotations

import json

from research_loop.api_budget import build_api_budget_report, compare_api_budget


def test_api_budget_detects_429_and_disconnects_from_local_files(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run.txt").write_text(
        "\n".join(
            [
                "[GT] 429 Too Many Requests",
                "Birdeye 404 token missing",
                "Birdeye 429 rate limit",
                "Jupiter 429 rate limit",
                "PumpPortal disconnect",
                "[RPC] getBalance error",
                "provider degraded",
                "cooldown active",
            ]
        ),
        encoding="utf-8",
    )
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "runtime_events.jsonl").write_text(
        json.dumps({"event_type": "provider_error", "provider": "gecko", "status_code": 429}) + "\n",
        encoding="utf-8",
    )

    report = build_api_budget_report(tmp_path)

    assert report["gecko_429_count"] == 2
    assert report["birdeye_404_count"] == 1
    assert report["birdeye_429_count"] == 1
    assert report["jupiter_rate_limit_count"] == 1
    assert report["pumpfun_disconnect_count"] == 1
    assert report["rpc_errors"] == 1
    assert report["cooldown_count"] == 1
    assert report["provider_degraded_minutes"] == 1
    assert (tmp_path / "data" / "research_runs" / "api_budget.json").exists()
    assert (tmp_path / "data" / "metrics" / "api_budget_report.json").exists()


def test_api_budget_compare_rejects_429_regression() -> None:
    comparison = compare_api_budget(
        {"gecko_429_count": 0, "birdeye_429_count": 0, "jupiter_rate_limit_count": 0},
        {"gecko_429_count": 1, "birdeye_429_count": 0, "jupiter_rate_limit_count": 0},
    )

    assert not comparison.ok
    assert comparison.deltas["api_429_count"] == 1
    assert "api_budget:api_429_count_delta>0" in comparison.rejection_reasons
