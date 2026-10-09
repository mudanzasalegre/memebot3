from __future__ import annotations

from types import SimpleNamespace

from analytics.moonshot_micro_lottery import (
    apply_moonshot_micro_lottery_context,
    evaluate_moonshot_micro_lottery,
    write_moonshot_micro_lottery_report,
)


def _token(**overrides):
    token = {
        "source": "pumpfun",
        "age_minutes": 2,
        "queue_age_minutes": 2,
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
    decision = evaluate_moonshot_micro_lottery(_token(txns_last_5m=320), dry_run=True, live=False)

    assert decision.allowed is True
    assert decision.reason == "confirmed_moonshot_buy"
    assert decision.amount_sol <= 0.001
    assert decision.route_proxy is True


def test_moonshot_relaxed_ultralow_signal_allows_known_mcap() -> None:
    decision = evaluate_moonshot_micro_lottery(
        _token(price_pct_5m=301, txns_last_5m=80, age_minutes=10, market_cap_usd=42_000),
        dry_run=True,
        live=False,
    )

    assert decision.allowed is True
    assert decision.reason == "confirmed_moonshot_buy"
    assert decision.amount_sol == 0.001


def test_moonshot_relaxed_ultralow_signal_requires_known_mcap() -> None:
    token = _token(price_pct_5m=301, txns_last_5m=80, age_minutes=10)
    token.pop("market_cap_usd")

    decision = evaluate_moonshot_micro_lottery(token, dry_run=True, live=False)

    assert decision.allowed is False
    assert "mcap_missing" in decision.failures


def test_moonshot_micro_lottery_blocks_live_without_flag_and_toxic() -> None:
    live = evaluate_moonshot_micro_lottery(_token(), dry_run=False, live=True)
    toxic = evaluate_moonshot_micro_lottery(_token(toxic_initial_sell_pressure=True), dry_run=True, live=False)

    assert live.allowed is False
    assert live.reason == "moonshot_live_disabled"
    assert toxic.allowed is False
    assert "toxic_initial_sell_pressure" in toxic.failures


def test_moonshot_micro_lottery_live_uses_live_flag() -> None:
    cfg = SimpleNamespace(MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED=True)
    decision = evaluate_moonshot_micro_lottery(_token(txns_last_5m=320), dry_run=False, live=True, cfg=cfg)

    assert decision.allowed is True
    assert decision.reason == "confirmed_moonshot_buy"


def test_moonshot_context_uses_own_lane_and_amount() -> None:
    token = _token()
    decision = evaluate_moonshot_micro_lottery(token, dry_run=True, live=False)
    apply_moonshot_micro_lottery_context(token, decision)

    assert token["entry_lane"] == "pump_early_moonshot_micro_lottery"
    assert token["gate_profile"] == "moonshot_micro_lottery"
    assert token["moonshot_micro_lottery_amount_sol"] == 0.001
    assert token["route_proxy"] == 1


def test_moonshot_honors_configured_amount_without_hidden_cap() -> None:
    cfg = SimpleNamespace(MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL=0.02)
    decision = evaluate_moonshot_micro_lottery(_token(txns_last_5m=320), dry_run=True, live=False, cfg=cfg)

    assert decision.allowed is True
    assert decision.amount_sol == 0.02


def test_moonshot_birth_velocity_probe_shadows_without_confirmation() -> None:
    decision = evaluate_moonshot_micro_lottery(
        _token(
            source="pumpfun",
            age_minutes=0.7,
                price_pct_5m=90,
                txns_last_5m=33,
                market_cap_usd=4_600,
                volume_24h_usd=1_500,
                has_jupiter_route=False,
                reason="green_sniper:paper_birth_probe:proxy_liquidity_productive_block,low_txns_5m",
            ),
        dry_run=True,
        live=False,
    )

    assert decision.allowed is False
    assert decision.reason == "moonshot_needs_confirmation:birth_velocity_shadow"
    assert decision.route_proxy is True


def test_moonshot_birth_velocity_probe_rejects_above_expanded_volume_band() -> None:
    decision = evaluate_moonshot_micro_lottery(
        _token(
            source="pumpfun",
            age_minutes=0.7,
            price_pct_5m=90,
            txns_last_5m=33,
            market_cap_usd=4_600,
                volume_24h_usd=3_001,
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

    assert decision.allowed is False
    assert decision.reason == "moonshot_needs_confirmation:late_proxy_shadow"


def test_moonshot_cluster_tail_probe_shadows_cluster_risk() -> None:
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

    assert decision.allowed is False
    assert "cluster_bad" in decision.failures
    assert decision.amount_sol == 0.001


def test_moonshot_cluster_tail_probe_uses_confirmed_shadow_move_for_micro_buy() -> None:
    cfg = SimpleNamespace(
        MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_BUY_ENABLED=True,
        MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_MODE_ENABLED=True,
        MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL=0.0005,
    )
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
            cluster_bad=True,
            reason="moonshot_micro_lottery_shadow:cluster_bad",
            observed_shadow_move_pct=80,
        ),
        dry_run=True,
        live=False,
        cfg=cfg,
    )

    assert decision.allowed is True
    assert decision.reason == "confirmed_moonshot_buy"
    assert decision.amount_sol == 0.0005


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


def test_moonshot_extreme_cluster_bad_override_shadows_proxy_liquidity() -> None:
    cfg = SimpleNamespace(
        MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_BUY_ENABLED=True,
        MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MIN_PRICE5M=500,
        MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MIN_TXNS_5M=80,
        MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MAX_AGE_MIN=10,
        MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_MODE_ENABLED=False,
    )

    decision = evaluate_moonshot_micro_lottery(
        _token(
            address="AwdSjSVx7cjHf2ShfxFuwosP8xrxnRkAeXp4Wd25pump",
            source="candidate_decision",
            age_minutes=2.97,
            price_pct_5m=6_978,
            txns_last_5m=171,
            market_cap_usd=143_529.29,
            liquidity_usd=1_200,
            cluster_bad=True,
            reason="moonshot_micro_lottery_shadow:cluster_bad",
        ),
        dry_run=True,
        live=False,
        cfg=cfg,
    )

    assert decision.allowed is False
    assert "cluster_bad" in decision.failures


def test_moonshot_extreme_cluster_bad_override_allows_real_liquidity_path() -> None:
    cfg = SimpleNamespace(
        MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_BUY_ENABLED=True,
        MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MIN_PRICE5M=500,
        MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MIN_TXNS_5M=80,
        MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MAX_AGE_MIN=10,
        MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_MODE_ENABLED=False,
    )

    decision = evaluate_moonshot_micro_lottery(
        _token(
            address="AwdSjSVx7cjHf2ShfxFuwosP8xrxnRkAeXp4Wd25pump",
            source="candidate_decision",
            age_minutes=2.97,
            price_pct_5m=6_978,
            txns_last_5m=171,
            market_cap_usd=143_529.29,
            liquidity_usd=21_200,
            liquidity_is_proxy=0,
            has_jupiter_route=True,
            price_impact_pct=6.0,
            cluster_bad=True,
            reason="moonshot_micro_lottery_shadow:cluster_bad",
        ),
        dry_run=True,
        live=False,
        cfg=cfg,
    )

    assert decision.allowed is True
    assert decision.reason == "confirmed_moonshot_buy:extreme_cluster_bad"
    assert decision.amount_sol == 0.001


def test_moonshot_extreme_cluster_bad_override_keeps_weak_clusters_shadowed() -> None:
    cfg = SimpleNamespace(
        MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_BUY_ENABLED=True,
        MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MIN_PRICE5M=500,
        MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MIN_TXNS_5M=80,
        MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MAX_AGE_MIN=10,
    )

    decision = evaluate_moonshot_micro_lottery(
        _token(
            address="WeakCluster111111111111111111111111111pump",
            price_pct_5m=499,
            txns_last_5m=120,
            market_cap_usd=42_000,
            cluster_bad=True,
        ),
        dry_run=True,
        live=False,
        cfg=cfg,
    )

    assert decision.allowed is False
    assert "cluster_bad" in decision.failures


def test_moonshot_risky_cluster_requires_ultralow_amount() -> None:
    allowed_cfg = SimpleNamespace(
        MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_MODE_ENABLED=True,
        MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL=0.0005,
    )
    blocked_cfg = SimpleNamespace(
        MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_MODE_ENABLED=True,
        MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL=0.0006,
    )

    allowed = evaluate_moonshot_micro_lottery(_token(cluster_bad=True), dry_run=True, live=False, cfg=allowed_cfg)
    blocked = evaluate_moonshot_micro_lottery(_token(cluster_bad=True), dry_run=True, live=False, cfg=blocked_cfg)

    assert allowed.allowed is True
    assert allowed.amount_sol == 0.0005
    assert blocked.allowed is False
    assert "cluster_bad" in blocked.failures


def test_moonshot_report_outputs_core_metrics(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "runtime_events.jsonl").write_text(
        (
            '{"address":"A","event_type":"actual_paper_buy","entry_lane":"pump_early_moonshot_micro_lottery",'
            '"reason":"confirmed_moonshot_buy"}\n'
            '{"address":"A","event_type":"buy","entry_lane":"pump_early_moonshot_micro_lottery",'
            '"reason":"confirmed_moonshot_buy"}\n'
        ),
        encoding="utf-8",
    )
    (metrics / "candidate_outcomes.jsonl").write_text(
        (
            '{"address":"A","entry_lane":"pump_early_moonshot_micro_lottery",'
            '"reason":"confirmed_moonshot_buy","route_proxy":1,"highest_pnl_pct":700,"total_pnl_pct":40,'
            '"source":"pumpfun","age_minutes":2,"queue_age_minutes":2,"txns_last_5m":320,"market_cap_usd":80000,'
            '"price_pct_5m":650,"has_jupiter_route":false}\n'
            '{"address":"B","source":"pumpfun","action":"shadow","reason":"moonshot_micro_lottery_shadow:cluster_bad",'
            '"price_pct_5m":350,"txns_last_5m":90,"market_cap_usd":50000,'
            '"age_minutes":4,"queue_age_minutes":4,"cluster_bad":true,"time_to_peak_sec":600,"max_pnl_pct":350}\n'
        ),
        encoding="utf-8",
    )
    (tmp_path / "data" / "paper_portfolio.json").write_text(
        (
            '{"positions":[{"address":"A","opened_at":"2026-05-22T10:01:00+00:00",'
            '"entry_lane":"pump_early_moonshot_micro_lottery","reason":"confirmed_moonshot_buy",'
            '"total_pnl_pct":40,"highest_pnl_pct":700}]}'
        ),
        encoding="utf-8",
    )

    report = write_moonshot_micro_lottery_report(tmp_path)

    assert report["buys"] == 1
    assert report["peak500_captured"] == 1
    assert report["confirmed_moonshot_buy"] == 1
    assert report["route_proxy_buys"] == 1
    assert report["risky_cluster_shadow"] == 1
    assert report["theoretical_moonshot_candidates"] == 2
    assert report["executable_moonshot_candidates"] == 1
    assert report["missed_moonshot_count"] == 1
    assert report["missed_peak100"] == 1
    assert report["moonshot_blockers"]["cluster_bad"] == 1
    assert report["moonshot_viability"]["theoretical_only"] == 1
    assert report["missed_moonshots"][0]["address"] == "B"
    assert report["missed_moonshots"][0]["time_to_peak_min"] == 10.0
