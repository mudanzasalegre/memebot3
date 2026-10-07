from __future__ import annotations

import json
import sqlite3
import pytest

from backtest.event_replay import build_event_replay


def _write_jsonl(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _buyable(address: str, first_seen_at: str, **overrides) -> dict:
    row = {
        "address": address,
        "source": "pumpfun",
        "first_seen_at": first_seen_at,
        "age_minutes": 2,
        "price_pct_5m": 650,
        "txns_last_5m": 320,
        "market_cap_usd": 80_000,
        "has_jupiter_route": True,
        "liquidity_is_proxy": False,
        "price_impact_pct": 0.0,
        "cluster_bad": False,
    }
    row.update(overrides)
    return row


def test_event_replay_does_not_use_future_peak_for_entry(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            {
                "address": "PEAK_ONLY",
                "source": "pumpfun",
                "first_seen_at": "2026-07-07T10:00:00+00:00",
                "closed_at": "2026-07-07T10:05:00+00:00",
                "price_pct_5m": 10,
                "txns_last_5m": 5,
                "market_cap_usd": 50_000,
                "max_pnl_pct": 1000,
                "pnl_pct": 80,
                "sample_type": "shadow_close",
            }
        ],
    )
    _write_jsonl(metrics / "runtime_events.jsonl", [])

    report = build_event_replay(
        tmp_path,
        replay_assumptions={"EVENT_REPLAY_LATENCY_SECONDS": 0, "EVENT_REPLAY_SLIPPAGE_BPS": 0},
    )

    assert report["metrics"]["closed_trades"] == 0
    assert report["metrics"]["missed_peak100_count"] == 1
    assert report["metrics"]["event_replay_lookahead_fields_ignored"] > 0


def test_event_replay_latency_can_expire_fast_outcome(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            _buyable(
                "FAST",
                "2026-07-07T10:00:00+00:00",
                closed_at="2026-07-07T10:00:10+00:00",
                pnl_pct=100,
                max_pnl_pct=120,
                sample_type="shadow_close",
            )
        ],
    )
    _write_jsonl(metrics / "runtime_events.jsonl", [])

    report = build_event_replay(
        tmp_path,
        replay_assumptions={"EVENT_REPLAY_LATENCY_SECONDS": 30, "EVENT_REPLAY_SLIPPAGE_BPS": 0},
    )

    assert report["metrics"]["closed_trades"] == 0
    assert report["metrics"]["event_replay_latency_expired"] == 1


def test_event_replay_dedupes_duplicate_open_mint(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            _buyable(
                "DUP",
                "2026-07-07T10:00:00+00:00",
                closed_at="2026-07-07T10:05:00+00:00",
                pnl_pct=20,
                max_pnl_pct=30,
                sample_type="shadow_close",
            ),
            _buyable(
                "DUP",
                "2026-07-07T10:00:01+00:00",
                closed_at="2026-07-07T10:05:00+00:00",
                pnl_pct=20,
                max_pnl_pct=30,
                sample_type="shadow_close",
            ),
        ],
    )
    _write_jsonl(metrics / "runtime_events.jsonl", [])

    report = build_event_replay(
        tmp_path,
        replay_assumptions={"EVENT_REPLAY_LATENCY_SECONDS": 0, "EVENT_REPLAY_SLIPPAGE_BPS": 0},
    )

    assert report["metrics"]["closed_trades"] == 1
    assert report["metrics"]["event_replay_duplicate_mints"] == 1


def test_event_replay_accounts_for_partial_fill_before_close(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            _buyable("PARTIAL", "2026-07-07T10:00:00+00:00"),
            {
                "address": "PARTIAL",
                "event_type": "candidate_outcome",
                "closed_at": "2026-07-07T10:02:00+00:00",
                "pnl_pct": -20,
                "max_pnl_pct": 45,
            },
        ],
    )
    _write_jsonl(
        metrics / "runtime_events.jsonl",
        [
            {
                "address": "PARTIAL",
                "event_type": "partial_fill",
                "ts_utc": "2026-07-07T10:01:00+00:00",
                "partial_fill_fraction": 0.5,
                "partial_pnl_pct": 40,
            }
        ],
    )

    report = build_event_replay(
        tmp_path,
        replay_assumptions={"EVENT_REPLAY_LATENCY_SECONDS": 0, "EVENT_REPLAY_SLIPPAGE_BPS": 0},
    )

    assert report["metrics"]["closed_trades"] == 1
    assert report["metrics"]["event_replay_partial_fills"] == 1
    assert report["trades"][0]["pnl_pct"] == 10.0


def test_event_replay_prefers_committed_trade_close_over_shadow_outcome(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            {
                **_buyable("ACTUAL", "2026-07-07T10:00:00+00:00"),
                "event_type": "candidate_decision",
                "decision_action": "bought",
                "entry_lane": "explicit_buy",
            },
            {
                "address": "ACTUAL",
                "event_type": "candidate_outcome",
                "source": "research_shadow",
                "ts_utc": "2026-07-07T10:03:00+00:00",
                "pnl_pct": 80.0,
                "exit_reason": "shadow_take_profit",
            },
        ],
    )
    _write_jsonl(metrics / "runtime_events.jsonl", [])
    db_path = tmp_path / "data" / "memebotdatabase.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table positions (
                id integer primary key,
                address text,
                closed integer,
                opened_at text,
                closed_at text,
                entry_lane text,
                total_pnl_pct real,
                total_pnl_usd real,
                exit_reason text
            )
            """
        )
        conn.execute(
            "insert into positions values (1, ?, 1, ?, ?, ?, ?, ?, ?)",
            (
                "ACTUAL",
                "2026-07-07T10:00:00+00:00",
                "2026-07-07T10:05:00+00:00",
                "pump_early_shadow_followup_micro",
                -42.5,
                -0.1,
                "LIQUIDITY_CRUSH",
            ),
        )

    report = build_event_replay(
        tmp_path,
        replay_assumptions={"EVENT_REPLAY_LATENCY_SECONDS": 0, "EVENT_REPLAY_SLIPPAGE_BPS": 0},
    )

    assert report["inputs"]["sqlite_closed_trades"] == 1
    assert report["inputs"]["conflicting_outcomes_suppressed"] == 1
    assert report["metrics"]["closed_trades"] == 1
    assert report["trades"][0]["pnl_pct"] == -42.5
    assert report["metrics"]["total_pnl_usd"] == -0.1
    assert report["trades"][0]["exit_reason"] == "LIQUIDITY_CRUSH"


def test_event_replay_uses_signal_timestamp_instead_of_retroactive_first_seen(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            {
                **_buyable("TIMED", "2026-07-07T10:00:00+00:00"),
                "event_type": "candidate_decision",
                "decision_action": "bought",
                "entry_lane": "explicit_buy",
                "ts_utc": "2026-07-07T10:05:00+00:00",
            },
            {
                "address": "TIMED",
                "event_type": "candidate_outcome",
                "ts_utc": "2026-07-07T10:06:00+00:00",
                "pnl_pct": 10,
            },
        ],
    )
    _write_jsonl(metrics / "runtime_events.jsonl", [])

    report = build_event_replay(
        tmp_path,
        replay_assumptions={"EVENT_REPLAY_LATENCY_SECONDS": 0, "EVENT_REPLAY_SLIPPAGE_BPS": 0},
    )

    assert report["trades"][0]["opened_at"] == "2026-07-07T10:05:00+00:00"


def test_event_replay_closed_false_with_pnl_is_not_terminal(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            {
                **_buyable("OPEN", "2026-07-07T10:00:00+00:00"),
                "event_type": "candidate_decision",
                "decision_action": "bought",
                "entry_lane": "explicit_buy",
                "closed": False,
                "pnl_pct": 10,
            }
        ],
    )
    _write_jsonl(metrics / "runtime_events.jsonl", [])

    report = build_event_replay(
        tmp_path,
        replay_assumptions={"EVENT_REPLAY_LATENCY_SECONDS": 0, "EVENT_REPLAY_SLIPPAGE_BPS": 0},
    )

    assert report["metrics"]["closed_trades"] == 0
    assert report["open_positions"] == 1


def test_event_replay_strict_route_mode_blocks_unknown_route(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    row = _buyable("NO_ROUTE", "2026-07-07T10:00:00+00:00")
    row.pop("has_jupiter_route")
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            {**row, "event_type": "candidate_decision", "decision_action": "bought", "entry_lane": "explicit_buy"},
            {"address": "NO_ROUTE", "event_type": "candidate_outcome", "ts_utc": "2026-07-07T10:05:00+00:00", "pnl_pct": 10},
        ],
    )
    _write_jsonl(metrics / "runtime_events.jsonl", [])

    report = build_event_replay(
        tmp_path,
        replay_assumptions={
            "EVENT_REPLAY_LATENCY_SECONDS": 0,
            "EVENT_REPLAY_SLIPPAGE_BPS": 0,
            "EVENT_REPLAY_ALLOW_ROUTE_PROXY": False,
        },
    )

    assert report["metrics"]["closed_trades"] == 0
    assert report["metrics"]["event_replay_route_blocked"] == 1


def test_event_replay_revalidates_full_bootstrap_quality_policy(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    address = "11111111111111111111111111111111"
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            {
                **_buyable(address, "2026-07-07T10:00:00+00:00"),
                "event_type": "candidate_decision",
                "decision_action": "bought",
                "entry_lane": "pump_early_paper_bootstrap_micro",
                "liquidity_usd": 100,
                "score_total": 45,
            },
            {
                "address": address,
                "event_type": "candidate_outcome",
                "ts_utc": "2026-07-07T10:05:00+00:00",
                "pnl_pct": 25,
            },
        ],
    )
    _write_jsonl(metrics / "runtime_events.jsonl", [])

    report = build_event_replay(
        tmp_path,
        candidate_config={
            "PAPER_BOOTSTRAP_ENABLED": True,
            "PAPER_BOOTSTRAP_QUALITY_GATES_ENABLED": True,
            "PAPER_BOOTSTRAP_REQUIRE_ROUTE": True,
            "PAPER_BOOTSTRAP_REQUIRE_REAL_LIQUIDITY": True,
            "PAPER_BOOTSTRAP_MIN_LIQUIDITY_USD": 1_500,
            "PAPER_BOOTSTRAP_MIN_MARKET_CAP_USD": 2_000,
            "PAPER_BOOTSTRAP_MIN_TXNS_5M": 25,
            "PAPER_BOOTSTRAP_MIN_SCORE_TOTAL": 30,
            "PAPER_BOOTSTRAP_MAX_SNAPSHOT_MISSING_FIELDS": 2,
        },
        replay_assumptions={
            "EVENT_REPLAY_LATENCY_SECONDS": 0,
            "EVENT_REPLAY_SLIPPAGE_BPS": 0,
            "EVENT_REPLAY_ALLOW_ROUTE_PROXY": False,
        },
    )

    assert report["metrics"]["closed_trades"] == 0
    assert report["metrics"]["simulated_buys"] == 0


@pytest.mark.parametrize("exact_mode", [False, True])
def test_event_replay_applies_final_lane_sizing_cap_to_bootstrap(tmp_path, exact_mode) -> None:
    metrics = tmp_path / "data" / "metrics"
    address = "So11111111111111111111111111111111111111112"
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            {
                **_buyable(address, "2026-07-07T10:00:00+00:00"),
                "event_type": "candidate_decision",
                "decision_action": "bought",
                "entry_lane": "pump_early_paper_bootstrap_micro",
                "price_usd": 0.00001,
                "liquidity_usd": 10_000,
                "score_total": 35,
                "entry_notional_usd": 3.0,
                "actual_buy_amount_sol": 0.03,
            },
            {
                "address": address,
                "event_type": "candidate_outcome",
                "ts_utc": "2026-07-07T10:05:00+00:00",
                "pnl_pct": 10,
            },
        ],
    )
    _write_jsonl(metrics / "runtime_events.jsonl", [])

    report = build_event_replay(
        tmp_path,
        candidate_config={
            "PAPER_BOOTSTRAP_ENABLED": True,
            "PAPER_BOOTSTRAP_AMOUNT_SOL": 0.1,
            "PAPER_BOOTSTRAP_MAX_AMOUNT_SOL": 0.1,
            "PAPER_MAX_TRADE_AMOUNT_SOL": 0.03,
            "PAPER_EXACT_TRADE_SIZE_ENABLED": exact_mode,
            "LANE_SIZING_ENABLED": True,
            "LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED": False,
        },
        replay_assumptions={
            "EVENT_REPLAY_LATENCY_SECONDS": 0,
            "EVENT_REPLAY_SLIPPAGE_BPS": 0,
            "EVENT_REPLAY_ALLOW_ROUTE_PROXY": False,
        },
    )

    if exact_mode:
        assert report["metrics"]["closed_trades"] == 0
        assert report["trades"] == []
        return
    assert report["metrics"]["closed_trades"] == 1
    assert report["trades"][0]["amount_sol"] == 0.03
    assert report["trades"][0]["entry_notional_usd"] == 3.0
    assert report["trades"][0]["pnl_usd"] == 0.3


@pytest.mark.parametrize("exact_mode,notional,pnl", [(False, 10.0, 1.0), (True, 100.0, 10.0)])
def test_event_replay_pnl_usd_uses_simulated_notional_and_percent_scale(tmp_path, exact_mode, notional, pnl) -> None:
    metrics = tmp_path / "data" / "metrics"
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            {
                **_buyable("NOTIONAL", "2026-07-07T10:00:00+00:00"),
                "event_type": "candidate_decision",
                "decision_action": "bought",
                "entry_lane": "explicit_buy",
                "entry_notional_usd": 10.0,
                "actual_buy_amount_sol": 0.01,
            },
            {
                "address": "NOTIONAL",
                "event_type": "candidate_outcome",
                "ts_utc": "2026-07-07T10:05:00+00:00",
                "pnl_pct": 10,
            },
        ],
    )
    _write_jsonl(metrics / "runtime_events.jsonl", [])

    report = build_event_replay(
        tmp_path,
        candidate_config={"PAPER_MAX_TRADE_AMOUNT_SOL": 0.1 if exact_mode else 0.01,
                          "PAPER_EXACT_TRADE_SIZE_ENABLED": exact_mode},
        replay_assumptions={
            "EVENT_REPLAY_LATENCY_SECONDS": 0,
            "EVENT_REPLAY_SLIPPAGE_BPS": 0,
        },
    )

    assert report["trades"][0]["entry_notional_usd"] == notional
    assert report["trades"][0]["pnl_usd"] == pnl
    assert report["metrics"]["total_pnl_usd"] == pnl


def test_event_replay_candidate_cannot_make_evaluator_assumptions_optimistic(tmp_path) -> None:
    _write_jsonl(tmp_path / "data" / "metrics" / "candidate_outcomes.jsonl", [])
    _write_jsonl(tmp_path / "data" / "metrics" / "runtime_events.jsonl", [])

    report = build_event_replay(
        tmp_path,
        candidate_config={
            "EVENT_REPLAY_LATENCY_SECONDS": 0,
            "EVENT_REPLAY_SLIPPAGE_BPS": 0,
            "EVENT_REPLAY_ALLOW_ROUTE_PROXY": False,
        },
    )

    assert report["config"]["latency_seconds"] == 30.0
    assert report["config"]["slippage_bps"] == 50.0
    assert report["config"]["allow_route_proxy"] is False
    assert report["config"]["ignored_candidate_replay_assumptions"] == [
        "EVENT_REPLAY_ALLOW_ROUTE_PROXY",
        "EVENT_REPLAY_LATENCY_SECONDS",
        "EVENT_REPLAY_SLIPPAGE_BPS",
    ]
