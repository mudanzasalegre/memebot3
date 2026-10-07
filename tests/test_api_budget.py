from __future__ import annotations

import json

from research_loop.api_budget import build_api_budget_report, compare_api_budget


def test_api_budget_detects_429_and_disconnects_from_local_files(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run.txt").write_text(
        "\n".join(
            [
                "[GT] HTTP 429 Too Many Requests",
                "Birdeye HTTP 404 token missing",
                "Birdeye HTTP 429 rate limit",
                "Jupiter HTTP 429 rate limit",
                "PumpPortal disconnect",
                "[RPC] getBalance error",
                "provider degraded",
                "Jupiter provider cooldown active",
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


def test_api_budget_ignores_429_digits_in_candidate_data(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run.txt").write_text(
        "\n".join(
            [
                "Jupiter quote ok mint=Token429 rank_score=42.9 ts=2026-07-10T22:29:42.900Z",
                "[GT] candidate price=0.00000429 address=429Mint success",
                "Birdeye token=/429Mint response ok",
                "RPC slot=429 request completed successfully",
                "lane cooldown active until 2026-07-10T22:42:09Z",
            ]
        ),
        encoding="utf-8",
    )
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    candidate = {
        "event_type": "candidate_decision",
        "source": "jupiter",
        "mint": "Address429",
        "timestamp": "2026-07-10T22:29:42.900Z",
        "rank_score": 42.9,
        "cooldown_until": "2026-07-10T22:42:09Z",
    }
    (metrics / "runtime_events.jsonl").write_text(json.dumps(candidate) + "\n", encoding="utf-8")

    report = build_api_budget_report(tmp_path, write=False)

    assert report["gecko_429_count"] == 0
    assert report["birdeye_429_count"] == 0
    assert report["jupiter_rate_limit_count"] == 0
    assert report["rpc_errors"] == 0
    assert report["cooldown_count"] == 0


def test_api_budget_uses_runtime_events_once_and_ignores_mirrors(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    signal = {
        "event_id": "provider-signal-1",
        "event_type": "provider_error",
        "provider": "jupiter",
        "status_code": 429,
    }
    (metrics / "runtime_events.jsonl").write_text(
        json.dumps(signal) + "\n" + json.dumps(signal) + "\n",
        encoding="utf-8",
    )
    for name in ("decision_ledger.jsonl", "candidate_outcomes.jsonl"):
        (metrics / name).write_text(json.dumps(signal) + "\n", encoding="utf-8")

    report = build_api_budget_report(tmp_path, write=False)

    assert report["jupiter_rate_limit_count"] == 1
    assert report["sources"]["duplicate_events_skipped"] == 1
    ignored_mirrors = {value.replace("\\", "/") for value in report["sources"]["ignored_mirror_files"]}
    assert ignored_mirrors == {
        "data/metrics/decision_ledger.jsonl",
        "data/metrics/candidate_outcomes.jsonl",
    }


def test_api_budget_counts_only_explicit_request_signals(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run.txt").write_text(
        "Jupiter quote cached successfully\nJupiter GET https://quote-api.jup.ag/v6/quote\n",
        encoding="utf-8",
    )
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        {"event_type": "candidate_decision", "source": "jupiter"},
        {"event_type": "provider_request", "provider": "jupiter"},
    ]
    (metrics / "runtime_events.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    report = build_api_budget_report(tmp_path, write=False)

    assert report["estimated_requests_by_provider"]["Jupiter"] == 2


def test_api_budget_detects_spanish_pumpfun_disconnect(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run.txt").write_text("PumpFun WS desconectado; reintentando\n", encoding="utf-8")

    report = build_api_budget_report(tmp_path, write=False)

    assert report["pumpfun_disconnect_count"] == 1


def test_api_budget_compare_rejects_429_regression() -> None:
    comparison = compare_api_budget(
        {"gecko_429_count": 0, "birdeye_429_count": 0, "jupiter_rate_limit_count": 0},
        {"gecko_429_count": 1, "birdeye_429_count": 0, "jupiter_rate_limit_count": 0},
    )

    assert not comparison.ok
    assert comparison.deltas["api_429_count"] == 1
    assert "api_budget:api_429_count_delta>0" in comparison.rejection_reasons
