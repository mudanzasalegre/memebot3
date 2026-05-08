import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from analytics.runner_jackpot_report import build_runner_jackpot_report


def test_runner_jackpot_report_summarizes_big_runners(tmp_path):
    outcomes = tmp_path / "candidate_outcomes.jsonl"
    rows = [
        {
            "address": "A",
            "symbol": "CHIMP",
            "entry_lane": "pump_early_sniper_research",
            "gate_profile": "pumpswap_profit_research",
            "profit_lane_tier": "pump_early_research_rank_canary",
            "max_pnl_pct_seen": 628,
            "total_pnl_pct": 500,
            "buy_price_pct_5m": 46,
            "buy_txns_last_5m": 784,
            "buy_liquidity_usd": 19_000,
            "buy_market_cap_usd": 61_000,
        },
        {
            "address": "B",
            "entry_lane": "pump_early_green_candle_sniper",
            "max_pnl_pct_seen": 80,
            "total_pnl_pct": -10,
        },
    ]
    outcomes.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    report = build_runner_jackpot_report(outcomes_path=outcomes, db_path=tmp_path / "missing.db")

    assert report["runners"]["runner_300"] == 1
    assert report["runners"]["runner_500"] == 1
    assert report["runners"]["runner_1000"] == 0
    assert report["top_runners"][0]["symbol"] == "CHIMP"
    assert report["recommended_profile"]["name"] == "jackpot_runner"
