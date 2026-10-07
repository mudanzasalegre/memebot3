from __future__ import annotations

import json

from analytics.causal_shadow_policy import build_causal_shadow_policy_audit


def _write_jsonl(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_causal_shadow_policy_uses_latest_pre_open_snapshot_and_freshness(tmp_path) -> None:
    run_id = "run-1"
    address = "mint-a"
    ledger = [
        {
            "run_id": run_id,
            "address": address,
            "ts_utc": "2026-07-13T12:00:00+00:00",
            "feature_snapshot": {
                "dex_id": "pumpswap",
                "has_jupiter_route": 1,
                "cluster_bad": 0,
                "snapshot_missing_fields": 7,
                "age_minutes": 20,
                "queue_age_minutes": 4,
                "liquidity_usd": 25_000,
                "market_cap_usd": 100_000,
                "txns_last_5m": 1_100,
                "score_total": 60,
                "price_impact_pct": 5,
                "price_pct_5m": -10,
            },
        },
        {
            "run_id": run_id,
            "address": address,
            "ts_utc": "2026-07-13T12:02:00+00:00",
            "feature_snapshot": {"dex_id": "raydium", "has_jupiter_route": 0},
        },
    ]
    outcomes = [
        {
            "run_id": run_id,
            "event_type": "candidate_outcome",
            "source": "research_shadow",
            "address": address,
            "opened_at": "2026-07-13T12:01:00+00:00",
            "pnl_pct": 25,
        }
    ]
    _write_jsonl(tmp_path / "data/metrics/decision_ledger.jsonl", ledger)
    _write_jsonl(tmp_path / "data/metrics/candidate_outcomes.normalized.jsonl", outcomes)

    report = build_causal_shadow_policy_audit(tmp_path, run_id=run_id)

    assert report["join_quality"]["matched_terminal_shadow_outcomes"] == 1
    assert report["forward_paper_candidate"]["outcomes"] == 1
    assert report["forward_paper_candidate"]["samples"][0]["dex_id"] == "pumpswap"
    assert report["forward_paper_candidate"]["samples"][0]["observed_optional_snapshot_missing_fields"] == 7
    diagnostic = report["forward_paper_candidate"]["optional_snapshot_missing_fields_diagnostic"]
    assert diagnostic["selection_gate"] is False
    assert diagnostic["max_observed"] == 7
    assert report["decision"]["live_ready"] is False


def test_causal_shadow_policy_rejects_stale_or_weak_negative_pullback(tmp_path) -> None:
    run_id = "run-2"
    ledger = []
    outcomes = []
    for index, (age, txns) in enumerate(((61, 1_100), (20, 999))):
        address = f"mint-{index}"
        ledger.append(
            {
                "run_id": run_id,
                "address": address,
                "ts_utc": "2026-07-13T12:00:00+00:00",
                "feature_snapshot": {
                    "dex_id": "pumpswap",
                    "has_jupiter_route": 1,
                    "cluster_bad": 0,
                    "age_minutes": age,
                    "queue_age_minutes": 4,
                    "liquidity_usd": 25_000,
                    "market_cap_usd": 100_000,
                    "txns_last_5m": txns,
                    "score_total": 60,
                    "price_impact_pct": 5,
                    "price_pct_5m": -10,
                },
            }
        )
        outcomes.append(
            {
                "run_id": run_id,
                "event_type": "candidate_outcome",
                "source": "research_shadow",
                "address": address,
                "opened_at": "2026-07-13T12:01:00+00:00",
                "pnl_pct": -10,
            }
        )
    _write_jsonl(tmp_path / "data/metrics/decision_ledger.jsonl", ledger)
    _write_jsonl(tmp_path / "data/metrics/candidate_outcomes.normalized.jsonl", outcomes)

    report = build_causal_shadow_policy_audit(tmp_path, run_id=run_id)

    assert report["pumpswap_route_no_cluster"]["outcomes"] == 2
    assert report["forward_paper_candidate"]["outcomes"] == 0
