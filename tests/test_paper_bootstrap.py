from __future__ import annotations

import json
from types import SimpleNamespace

from analytics.paper_bootstrap import apply_paper_bootstrap_context, build_paper_bootstrap_report, should_allow_paper_bootstrap


def _row() -> dict[str, object]:
    return {
        "address": "So11111111111111111111111111111111111111112",
        "chain": "solana",
        "price_usd": 0.00001,
        "liquidity_usd": 10_000,
        "market_cap_usd": 25_000,
        "txns_last_5m": 25,
        "score_total": 35,
        "has_jupiter_route": True,
        "liquidity_usd_is_proxy": False,
        "cluster_bad": False,
        "price_impact_pct": 0.0,
        "price_pct_5m": 0.0,
    }


def _decision(**overrides: object):
    row = overrides.pop("row", _row())
    kwargs = {
        "dry_run": True,
        "live": False,
        "open_count": 99,
        "daily_buys": 99,
        "hourly_buys": 99,
        "seconds_since_last_buy": 0.0,
        "closed_trades": 0,
        "model_loaded": False,
        "model_rows": 0,
        "trigger_stage": "test",
        "trigger_reason": "test",
        "cfg": SimpleNamespace(
            PAPER_BOOTSTRAP_MAX_OPEN=0,
            PAPER_BOOTSTRAP_MAX_DAILY_BUYS=0,
            PAPER_BOOTSTRAP_MAX_HOURLY_BUYS=0,
            PAPER_BOOTSTRAP_MIN_SECONDS_BETWEEN_BUYS=0,
        ),
    }
    kwargs.update(overrides)
    return should_allow_paper_bootstrap(row, **kwargs)


def test_paper_bootstrap_zero_caps_are_unlimited() -> None:
    decision = _decision()

    assert decision.allowed is True
    assert decision.reason == "paper_bootstrap"


def test_paper_bootstrap_positive_caps_still_block_when_configured() -> None:
    decision = _decision(
        cfg=SimpleNamespace(
            PAPER_BOOTSTRAP_MAX_OPEN=1,
            PAPER_BOOTSTRAP_MAX_DAILY_BUYS=0,
            PAPER_BOOTSTRAP_MAX_HOURLY_BUYS=0,
            PAPER_BOOTSTRAP_MIN_SECONDS_BETWEEN_BUYS=0,
        )
    )

    assert decision.allowed is False
    assert decision.reason == "paper_bootstrap_open_cap"


def test_paper_bootstrap_closed_trades_mark_cold_start_complete_even_if_model_unloaded() -> None:
    decision = _decision(
        closed_trades=50,
        model_loaded=False,
        model_rows=0,
        cfg=SimpleNamespace(
            PAPER_BOOTSTRAP_REQUIRE_COLD_START=True,
            PAPER_BOOTSTRAP_MAX_OPEN=0,
            PAPER_BOOTSTRAP_MAX_DAILY_BUYS=0,
            PAPER_BOOTSTRAP_MAX_HOURLY_BUYS=0,
            PAPER_BOOTSTRAP_MIN_SECONDS_BETWEEN_BUYS=0,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == "paper_bootstrap_cold_start_complete"
    assert decision.model_cold is False


def test_paper_bootstrap_default_keeps_running_after_cold_start() -> None:
    decision = _decision(closed_trades=50, model_loaded=False, model_rows=0)

    assert decision.allowed is True
    assert decision.reason == "paper_bootstrap"
    assert decision.model_cold is False


def test_paper_bootstrap_quality_gate_blocks_proxy_liquidity() -> None:
    row = {**_row(), "liquidity_usd_is_proxy": True}
    kwargs = {
        "dry_run": True,
        "live": False,
        "open_count": 0,
        "daily_buys": 0,
        "hourly_buys": 0,
        "seconds_since_last_buy": 999.0,
        "closed_trades": 0,
        "model_loaded": False,
        "model_rows": 0,
        "trigger_stage": "test",
        "trigger_reason": "test",
        "cfg": SimpleNamespace(),
    }

    decision = should_allow_paper_bootstrap(row, **kwargs)

    assert decision.allowed is False
    assert decision.reason == "paper_bootstrap_hard_risk"
    assert "proxy_liquidity" in decision.hard_failures


def test_paper_bootstrap_real_liquidity_requires_explicit_proxy_flag() -> None:
    unknown_rows = [
        {k: v for k, v in _row().items() if k != "liquidity_usd_is_proxy"},
        {**_row(), "liquidity_usd_is_proxy": None},
        {**_row(), "liquidity_usd_is_proxy": "   "},
    ]
    explicit_false = _decision(row={**_row(), "liquidity_usd_is_proxy": "false"})
    explicit_true = _decision(row={**_row(), "liquidity_usd_is_proxy": "true"})

    for row in unknown_rows:
        decision = _decision(row=row)
        assert decision.allowed is False
        assert "liquidity_proxy_unknown" in decision.hard_failures
    assert explicit_false.allowed is True
    assert explicit_true.allowed is False
    assert "proxy_liquidity" in explicit_true.hard_failures


def test_paper_bootstrap_cluster_policy_requires_explicit_status() -> None:
    cfg = SimpleNamespace(PAPER_BOOTSTRAP_BLOCK_CLUSTER_BAD=True)
    unknown_rows = [
        {k: v for k, v in _row().items() if k != "cluster_bad"},
        {**_row(), "cluster_bad": None},
        {**_row(), "cluster_bad": "   "},
    ]
    explicit_false = _decision(row={**_row(), "cluster_bad": 0}, cfg=cfg)
    explicit_true = _decision(row={**_row(), "cluster_bad": 1}, cfg=cfg)

    for row in unknown_rows:
        decision = _decision(row=row, cfg=cfg)
        assert decision.allowed is False
        assert "cluster_status_unknown" in decision.hard_failures
    assert explicit_false.allowed is True
    assert explicit_true.allowed is False
    assert "cluster_bad" in explicit_true.hard_failures


def test_paper_bootstrap_requires_observed_price_impact_when_capped() -> None:
    cfg = SimpleNamespace(PAPER_BOOTSTRAP_MAX_PRICE_IMPACT_PCT=12)
    missing = _decision(row={k: v for k, v in _row().items() if k != "price_impact_pct"}, cfg=cfg)
    explicit_zero = _decision(row={**_row(), "price_impact_pct": 0}, cfg=cfg)

    assert missing.allowed is False
    assert "price_impact_missing" in missing.hard_failures
    assert explicit_zero.allowed is True


def test_paper_bootstrap_requires_observed_queue_age_when_capped() -> None:
    cfg = SimpleNamespace(PAPER_BOOTSTRAP_MAX_QUEUE_AGE_MIN=15)
    missing = _decision(cfg=cfg)
    explicit_zero = _decision(row={**_row(), "queue_age_minutes": 0}, cfg=cfg)

    assert missing.allowed is False
    assert "queue_age_missing" in missing.hard_failures
    assert explicit_zero.allowed is True


def test_paper_bootstrap_requires_observed_price5m_and_accepts_zero() -> None:
    missing = _decision(row={k: v for k, v in _row().items() if k != "price_pct_5m"})
    explicit_zero = _decision(row={**_row(), "price_pct_5m": 0})

    assert missing.allowed is False
    assert "price5m_missing" in missing.hard_failures
    assert explicit_zero.allowed is True


def test_paper_bootstrap_forward_lane_requires_pumpswap_and_freshness() -> None:
    cfg = SimpleNamespace(
        PAPER_BOOTSTRAP_AMOUNT_SOL=0.1,
        PAPER_BOOTSTRAP_MAX_AMOUNT_SOL=0.1,
        PAPER_BOOTSTRAP_REQUIRE_EXACT_AMOUNT=True,
        PAPER_BOOTSTRAP_REQUIRE_PUMPSWAP=True,
        PAPER_BOOTSTRAP_BLOCK_CLUSTER_BAD=True,
        PAPER_BOOTSTRAP_MAX_AGE_MIN=60,
        PAPER_BOOTSTRAP_MAX_QUEUE_AGE_MIN=15,
        PAPER_BOOTSTRAP_MIN_LIQUIDITY_USD=20_000,
        PAPER_BOOTSTRAP_MIN_MARKET_CAP_USD=50_000,
        PAPER_BOOTSTRAP_MIN_TXNS_5M=100,
        PAPER_BOOTSTRAP_MIN_SCORE_TOTAL=50,
        PRE_ENTRY_RISK_MIN_REAL_LIQUIDITY_USD=20_000,
        PRE_ENTRY_RISK_MIN_MCAP_USD=50_000,
        PRE_ENTRY_RISK_MIN_TXNS_5M=100,
    )
    kwargs = {
        "dry_run": True,
        "live": False,
        "open_count": 0,
        "daily_buys": 0,
        "hourly_buys": 0,
        "seconds_since_last_buy": 999.0,
        "closed_trades": 0,
        "model_loaded": False,
        "model_rows": 0,
        "trigger_stage": "test",
        "trigger_reason": "test",
        "cfg": cfg,
    }
    base = {
        **_row(),
        "dex_id": "pumpswap",
        "age_minutes": 42,
        "queue_age_minutes": 10,
        "liquidity_usd": 25_000,
        "market_cap_usd": 100_000,
        "txns_last_5m": 150,
        "score_total": 55,
    }

    allowed = should_allow_paper_bootstrap(base, **kwargs)
    wrong_dex = should_allow_paper_bootstrap({**base, "dex_id": "raydium"}, **kwargs)
    stale_token = should_allow_paper_bootstrap({**base, "age_minutes": 61}, **kwargs)
    stale_queue = should_allow_paper_bootstrap({**base, "queue_age_minutes": 16}, **kwargs)

    assert allowed.allowed is True
    assert allowed.amount_sol == 0.1
    assert "not_pumpswap" in wrong_dex.hard_failures
    assert "age_above_max" in stale_token.hard_failures
    assert "queue_age_above_max" in stale_queue.hard_failures


def test_paper_bootstrap_exact_amount_blocks_risk_downsize() -> None:
    cfg = SimpleNamespace(
        PAPER_BOOTSTRAP_AMOUNT_SOL=0.1,
        PAPER_BOOTSTRAP_MAX_AMOUNT_SOL=0.1,
        PAPER_BOOTSTRAP_REQUIRE_EXACT_AMOUNT=True,
        PRE_ENTRY_RISK_MIN_TXNS_5M=100,
    )

    decision = _decision(cfg=cfg)

    assert decision.allowed is False
    assert decision.reason == "paper_bootstrap_exact_amount_required"
    assert decision.amount_sol == 0.1


def test_paper_bootstrap_post_probe_route_requirement_is_fail_closed() -> None:
    kwargs = {
        "dry_run": True,
        "live": False,
        "open_count": 0,
        "daily_buys": 0,
        "hourly_buys": 0,
        "seconds_since_last_buy": 999.0,
        "closed_trades": 0,
        "model_loaded": False,
        "model_rows": 0,
        "trigger_stage": "post_risk_enrichment",
        "trigger_reason": "route_probe_complete",
        "require_observed_route": True,
        "cfg": SimpleNamespace(PAPER_BOOTSTRAP_REQUIRE_ROUTE=True),
    }

    missing = should_allow_paper_bootstrap({k: v for k, v in _row().items() if k != "has_jupiter_route"}, **kwargs)
    false = should_allow_paper_bootstrap({**_row(), "has_jupiter_route": False}, **kwargs)
    true = should_allow_paper_bootstrap(_row(), **kwargs)

    assert missing.allowed is False
    assert false.allowed is False
    assert "no_jupiter_route" in missing.hard_failures
    assert "no_jupiter_route" in false.hard_failures
    assert true.allowed is True


def test_paper_bootstrap_context_uses_decision_route_policy() -> None:
    decision = _decision(cfg=SimpleNamespace(PAPER_BOOTSTRAP_REQUIRE_ROUTE=False))
    row = _row()

    apply_paper_bootstrap_context(row, decision)

    assert decision.require_route is False
    assert row["require_jupiter_for_buy"] == 0


def test_paper_bootstrap_blocks_pre_entry_no_pump_shape() -> None:
    row = {
        **_row(),
        "price_pct_5m": 5.61,
        "market_cap_usd": 465_914.0,
        "txns_last_5m": 866,
        "liquidity_usd": 53_254.2,
    }
    kwargs = {
        "dry_run": True,
        "live": False,
        "open_count": 0,
        "daily_buys": 0,
        "hourly_buys": 0,
        "seconds_since_last_buy": 999.0,
        "closed_trades": 0,
        "model_loaded": False,
        "model_rows": 0,
        "trigger_stage": "test",
        "trigger_reason": "test",
        "cfg": SimpleNamespace(),
    }

    decision = should_allow_paper_bootstrap(row, **kwargs)

    assert decision.allowed is False
    assert decision.reason == "paper_bootstrap_pre_entry_risk"
    assert "high_mcap_no_pump" in decision.hard_failures


def test_paper_bootstrap_report_counts_real_buys_once(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        {
            "event_type": "actual_paper_buy",
            "address": "DUP",
            "entry_lane": "pump_early_paper_bootstrap_micro",
        },
        {
            "event_type": "buy",
            "address": "DUP",
            "entry_lane": "pump_early_paper_bootstrap_micro",
        },
    ]
    (metrics / "runtime_events.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    (tmp_path / "data" / "paper_portfolio.json").write_text(
        json.dumps(
            {
                "positions": [
                    {
                        "address": "DUP",
                        "opened_at": "2026-05-22T10:01:00+00:00",
                        "entry_lane": "pump_early_paper_bootstrap_micro",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = build_paper_bootstrap_report(tmp_path)

    assert report["actual_paper_buys"] == 1
