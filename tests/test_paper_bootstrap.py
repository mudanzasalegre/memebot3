from __future__ import annotations

import json
from types import SimpleNamespace

from analytics.paper_bootstrap import build_paper_bootstrap_report, should_allow_paper_bootstrap


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
    }


def _decision(**overrides: object):
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
    return should_allow_paper_bootstrap(_row(), **kwargs)


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
