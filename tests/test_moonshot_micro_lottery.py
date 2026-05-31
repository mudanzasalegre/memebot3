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


def test_moonshot_birth_velocity_probe_allows_moderate_birth_runner() -> None:
    decision = evaluate_moonshot_micro_lottery(
        _token(
            source="pumpfun",
            age_minutes=0.7,
            price_pct_5m=90,
            txns_last_5m=33,
            market_cap_usd=4_600,
            volume_24h_usd=1_000,
            has_jupiter_route=False,
            reason="green_sniper:paper_birth_probe:proxy_liquidity_productive_block,low_txns_5m",
        ),
        dry_run=True,
        live=False,
    )

    assert decision.allowed is True
    assert decision.reason == "moonshot_birth_velocity_probe"
    assert decision.route_proxy is True


def test_moonshot_birth_velocity_probe_rejects_overheated_volume_band() -> None:
    decision = evaluate_moonshot_micro_lottery(
        _token(
            source="pumpfun",
            age_minutes=0.7,
            price_pct_5m=90,
            txns_last_5m=33,
            market_cap_usd=4_600,
            volume_24h_usd=3_000,
            has_jupiter_route=False,
            reason="green_sniper:paper_birth_probe:proxy_liquidity_productive_block,low_txns_5m",
        ),
        dry_run=True,
        live=False,
    )

    assert decision.allowed is False
    assert "not_extreme_momentum" in decision.failures


def test_moonshot_late_proxy_momentum_allows_low_txns_tail_shape() -> None:
    decision = evaluate_moonshot_micro_lottery(
        _token(
            source="pumpfun",
            age_minutes=7,
            price_pct_5m=685,
            txns_last_5m=30,
            market_cap_usd=18_700,
            has_jupiter_route=False,
        ),
        dry_run=True,
        live=False,
    )

    assert decision.allowed is True
    assert decision.reason == "moonshot_late_proxy_momentum"


def test_moonshot_cluster_tail_probe_allows_micro_paper_cluster_risk() -> None:
    decision = evaluate_moonshot_micro_lottery(
        _token(
            age_minutes=4,
            price_pct_5m=35,
            txns_last_5m=25,
            liquidity_usd=22_000,
            market_cap_usd=97_000,
            volume_24h_usd=33_070,
            cluster_bad=True,
        ),
        dry_run=True,
        live=False,
    )

    assert decision.allowed is True
    assert decision.reason == "moonshot_cluster_tail_probe"
    assert decision.amount_sol == 0.001


def test_moonshot_cluster_tail_probe_uses_reason_and_pumpfun_mint_when_source_is_telemetry() -> None:
    decision = evaluate_moonshot_micro_lottery(
        _token(
            address="J1g1Lquz9TtNjXRJeE36geHEaCqKqgf58qT3hvBKpump",
            source="candidate_decision",
            age_minutes=3.7,
            price_pct_5m=0,
            txns_last_5m=0,
            liquidity_usd=12_549,
            market_cap_usd=26_308,
            volume_24h_usd=67_596,
            cluster_bad=False,
            reason="moonshot_micro_lottery_shadow:cluster_bad",
        ),
        dry_run=True,
        live=False,
    )

    assert decision.allowed is True
    assert decision.reason == "moonshot_cluster_tail_probe"


def test_moonshot_cluster_bad_outside_tail_shape_still_shadows() -> None:
    decision = evaluate_moonshot_micro_lottery(
        _token(
            age_minutes=8,
            price_pct_5m=35,
            txns_last_5m=25,
            liquidity_usd=2_000,
            market_cap_usd=97_000,
            volume_24h_usd=5_000,
            cluster_bad=True,
        ),
        dry_run=True,
        live=False,
    )

    assert decision.allowed is False
    assert "cluster_bad" in decision.failures


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
