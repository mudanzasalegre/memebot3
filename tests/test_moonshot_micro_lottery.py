from __future__ import annotations

from analytics.moonshot_micro_lottery import (
    apply_moonshot_micro_lottery_context,
    evaluate_moonshot_micro_lottery,
    write_moonshot_micro_lottery_report,
)


def _token(**overrides):
    token = {
        "source": "pumpfun",
        "age_minutes": 2,
        "txns_last_5m": 120,
        "market_cap_usd": 80_000,
        "price_pct_5m": 650,
        "has_jupiter_route": False,
        "toxic_initial_sell_pressure": False,
        "cluster_bad": False,
    }
    token.update(overrides)
    return token


def test_moonshot_micro_lottery_allows_paper_only_route_proxy() -> None:
    decision = evaluate_moonshot_micro_lottery(_token(), dry_run=True, live=False)

    assert decision.allowed is True
    assert decision.amount_sol <= 0.005
    assert decision.route_proxy is True


def test_moonshot_micro_lottery_blocks_live_and_toxic() -> None:
    live = evaluate_moonshot_micro_lottery(_token(), dry_run=False, live=True)
    toxic = evaluate_moonshot_micro_lottery(_token(toxic_initial_sell_pressure=True), dry_run=True, live=False)

    assert live.allowed is False
    assert live.reason == "moonshot_paper_only"
    assert toxic.allowed is False
    assert "toxic_initial_sell_pressure" in toxic.failures


def test_moonshot_context_uses_own_lane_and_amount() -> None:
    token = _token()
    decision = evaluate_moonshot_micro_lottery(token, dry_run=True, live=False)
    apply_moonshot_micro_lottery_context(token, decision)

    assert token["entry_lane"] == "pump_early_moonshot_micro_lottery"
    assert token["gate_profile"] == "moonshot_micro_lottery"
    assert token["moonshot_micro_lottery_amount_sol"] == 0.002
    assert token["route_proxy"] == 1


def test_moonshot_report_outputs_core_metrics(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(
        '{"address":"A","entry_lane":"pump_early_moonshot_micro_lottery","highest_pnl_pct":700,"total_pnl_pct":40}\n',
        encoding="utf-8",
    )

    report = write_moonshot_micro_lottery_report(tmp_path)

    assert report["buys"] == 1
    assert report["peak500_captured"] == 1
