from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_ORIG_ANALYTICS = sys.modules.get("analytics")
_ORIG_RESEARCH_RUNTIME = sys.modules.get("analytics.research_runtime")

_analytics_stub = types.ModuleType("analytics")
_analytics_stub.__path__ = []  # type: ignore[attr-defined]
_research_runtime_stub = types.ModuleType("analytics.research_runtime")
_research_runtime_stub.load_live_rank_gate = lambda *args, **kwargs: {}
_analytics_stub.research_runtime = _research_runtime_stub
sys.modules["analytics"] = _analytics_stub
sys.modules["analytics.research_runtime"] = _research_runtime_stub

_MODULE_PATH = _ROOT / "analytics" / "strategy_runtime.py"
_SPEC = importlib.util.spec_from_file_location("strategy_runtime_under_test", _MODULE_PATH)
assert _SPEC and _SPEC.loader
strategy_runtime = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = strategy_runtime
_SPEC.loader.exec_module(strategy_runtime)

if _ORIG_ANALYTICS is not None:
    sys.modules["analytics"] = _ORIG_ANALYTICS
else:
    sys.modules.pop("analytics", None)
if _ORIG_RESEARCH_RUNTIME is not None:
    sys.modules["analytics.research_runtime"] = _ORIG_RESEARCH_RUNTIME
else:
    sys.modules.pop("analytics.research_runtime", None)


def _make_cfg(**overrides: object) -> SimpleNamespace:
    base = {
        "STRATEGY_REGIME_MODE_DEFAULT": "shadow",
        "PUMP_EARLY_EXECUTION_MODE": "live",
        "DEX_MATURE_EXECUTION_MODE": "shadow",
        "REVIVAL_EXECUTION_MODE": "shadow",
        "PUMP_EARLY_CONFIRM_SNAPSHOTS": 1,
        "PUMP_EARLY_CONFIRM_BACKOFF_S": 30,
        "PUMP_EARLY_CONFIRM_MIN_AGE_MIN": 0.0,
        "DEX_MATURE_CONFIRM_SNAPSHOTS": 1,
        "DEX_MATURE_CONFIRM_BACKOFF_S": 30,
        "DEX_MATURE_CONFIRM_MIN_AGE_MIN": 0.0,
        "REVIVAL_CONFIRM_SNAPSHOTS": 1,
        "REVIVAL_CONFIRM_BACKOFF_S": 30,
        "REVIVAL_CONFIRM_MIN_AGE_MIN": 0.0,
        "STRATEGY_CONFIRMATION_ENABLED": True,
        "STRATEGY_CONFIRM_DEFAULT_SNAPSHOTS": 1,
        "STRATEGY_CONFIRM_REQUIRE_ROUTE": False,
        "STRATEGY_CONFIRM_LIQUIDITY_DROP_PCT": 20.0,
        "REGIME_HEALTH_WINDOW_TRADES": 20,
        "REGIME_HEALTH_WINDOW_EVENTS": 40,
        "REGIME_HEALTH_MIN_TRADES": 6,
        "REGIME_HEALTH_DISABLE_EXPECTANCY_PCT": -5.0,
        "REGIME_HEALTH_RECOVERY_EXPECTANCY_PCT": 1.0,
        "REGIME_HEALTH_MAX_CONSECUTIVE_LOSSES": 4,
        "REGIME_HEALTH_MIN_EXEC_SUCCESS_RATE": 0.7,
        "REGIME_HEALTH_MIN_PRICE_COVERAGE_RATE": 0.7,
        "REGIME_HEALTH_COOLDOWN_MIN": 120,
        "REGIME_HEALTH_DISABLE_ACTION": "shadow",
        "REGIME_HEALTH_COOLDOWN_MAX_SIZE_MULTIPLIER": 0.10,
        "REGIME_RECOVERY_MAX_SIZE_MULTIPLIER": 0.10,
        "PUMP_EARLY_RECOVERY_MAX_SIZE_MULTIPLIER": 0.10,
        "DEX_MATURE_RECOVERY_MAX_SIZE_MULTIPLIER": 0.10,
        "REVIVAL_RECOVERY_MAX_SIZE_MULTIPLIER": 0.10,
        "SIZE_MIN_MULTIPLIER": 0.10,
        "STRATEGY_SCORECARD_OVERRIDE_ENABLED": True,
        "STRATEGY_SCORECARD_MIN_OUTCOMES": 12,
        "STRATEGY_SCORECARD_MAX_AGE_MIN": 240.0,
        "STRATEGY_SCORECARD_DEMOTE_MAX_AVG_PNL_PCT": -1.0,
        "PUMP_EARLY_RECOVERY_MIN_WIN_RATE_PCT": 42.0,
        "DRY_RUN": True,
        "PUMP_EARLY_SNIPER_ENABLED": False,
        "PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_AGE_MIN": 3.0,
        "PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_TXNS_5M": 40,
        "PUMP_EARLY_SNIPER_FAST_CONFIRM_BACKOFF_S": 10,
        "PUMP_EARLY_SNIPER_DEMOTE_LOSS_STREAK": 4,
        "PUMP_EARLY_SNIPER_DEMOTE_WINDOW_TRADES": 8,
        "PUMP_EARLY_SNIPER_DEMOTE_AVG_PNL_PCT": -5.0,
        "PUMP_EARLY_SNIPER_DEMOTE_LIQ_CRUSH_FIRST_CLOSES": 10,
        "PUMP_EARLY_SNIPER_DEMOTE_LIQ_CRUSH_ROLLING": 2,
        "PUMP_EARLY_SNIPER_CANARY_INITIAL_CLOSES": 10,
        "PUMP_EARLY_SNIPER_CANARY_INITIAL_SIZE_CAP": 0.20,
        "PUMP_EARLY_SNIPER_PAPER_CONTINUE_ON_HEALTH": True,
        "PUMP_EARLY_SNIPER_PAPER_RECOVERY_SIZE_CAP": 0.20,
        "PAPER_PNL_STRICT_HEALTH": True,
        "PUMP_EARLY_RECOVERY_RECENT_OVERRIDE_ENABLED": True,
        "PUMP_EARLY_RECOVERY_RECENT_IGNORE_OLD_LIQ_CRUSH": True,
        "PUMP_EARLY_SHADOW_RECOVERY_ENABLED": False,
        "PUMP_EARLY_SHADOW_RECOVERY_WINDOW": 8,
        "PUMP_EARLY_SHADOW_RECOVERY_MIN_TRADES": 8,
        "PUMP_EARLY_SHADOW_RECOVERY_MIN_AVG_PNL_PCT": 5.0,
        "PUMP_EARLY_SHADOW_RECOVERY_MIN_WIN_RATE_PCT": 45.0,
        "PUMP_EARLY_SHADOW_RECOVERY_MAX_SEVERE_EXITS": 2,
        "PUMP_EARLY_SHADOW_RECOVERY_MAX_LIQ_CRUSH": 1,
        "PUMP_EARLY_SHADOW_RECOVERY_MAX_CONSECUTIVE_LOSSES": 3,
        "PUMP_EARLY_SHADOW_RECOVERY_MAX_AGE_H": 36.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _rank_gate(
    *,
    ready: bool = False,
    source: str = "fallback",
    selected: int = 0,
    avg_realized: float | None = None,
) -> dict[str, object]:
    return {
        "regime": "pump_early",
        "enabled": True,
        "source": source,
        "threshold": 12.5,
        "fallback_threshold": 12.5,
        "activation_ready": ready,
        "generated_at_utc": dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc).isoformat(),
        "stale": False,
        "selected_rows_at_picked": selected,
        "avg_realized_pnl_pct_at_picked": avg_realized,
    }


def _reset_strategy_state() -> None:
    strategy_runtime._SCORECARD_CACHE = None
    strategy_runtime._SCORECARD_MTIME_NS = None
    strategy_runtime._SHADOW_RECOVERY_CACHE = None
    strategy_runtime._SHADOW_RECOVERY_MTIME_NS = None
    strategy_runtime._SHADOW_RECOVERY_SIZE = None
    strategy_runtime._CANDIDATES.clear()
    strategy_runtime._BUCKET_HEALTH.clear()
    for health in strategy_runtime._HEALTH.values():
        health.trade_pnls_pct.clear()
        health.trade_wins.clear()
        health.exec_success.clear()
        health.price_coverage.clear()
        health.severe_exits.clear()
        health.liq_crush_exits.clear()
        health.recovery_trade_pnls_pct.clear()
        health.recovery_trade_wins.clear()
        health.recovery_severe_exits.clear()
        health.recovery_liq_crush_exits.clear()
        health.consecutive_losses = 0
        health.recovery_consecutive_losses = 0
        health.cooldown_until = None
        health.last_disable_reason = None
        health.recovery_armed = False
        health.canary_active = False
        health.last_auto_demote_at = None
        health.last_auto_recover_at = None


def _write_scorecard(path: Path, *, generated_at: dt.datetime, group: str, count: int, avg_pnl_pct: float, win_rate_pct: float) -> None:
    path.write_text(
        json.dumps(
            {
                "generated_at_utc": generated_at.isoformat(),
                "outcomes_by_regime": [
                    {
                        "group": group,
                        "count": count,
                        "avg_pnl_pct": avg_pnl_pct,
                        "win_rate_pct": win_rate_pct,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_shadow_recovery_events(path: Path, *, now: dt.datetime, pnls: list[float], exits: list[str]) -> None:
    rows: list[str] = []
    for idx, (pnl, exit_reason) in enumerate(zip(pnls, exits, strict=False)):
        rows.append(
            json.dumps(
                {
                    "ts_utc": (now - dt.timedelta(minutes=len(pnls) - idx)).isoformat(),
                    "event_type": "candidate_outcome",
                    "address": f"shadow-{idx}",
                    "regime": "pump_early",
                    "source": "research_shadow",
                    "shadow_kind": "execution",
                    "reason": "strategy:recovery_not_ready",
                    "pnl_pct": pnl,
                    "exit_reason": exit_reason,
                    "venue_is_pumpswap": 1,
                    "liquidity_is_proxy": 0,
                    "liquidity_usd": 12_000.0,
                    "market_cap_usd": 18_000.0,
                    "age_minutes": 6.0,
                    "score_total": 45,
                    "price5m_bucket": "<0",
                }
            )
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_negative_scorecard_demotes_live_pump_to_shadow(tmp_path: Path, monkeypatch) -> None:
    now = dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc)
    scorecard_path = tmp_path / "research_scorecard.json"
    _write_scorecard(scorecard_path, generated_at=now, group="pump_early", count=24, avg_pnl_pct=-8.5, win_rate_pct=25.0)

    monkeypatch.setattr(strategy_runtime, "CFG", _make_cfg())
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", scorecard_path)
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    decision = strategy_runtime.evaluate_candidate(
        {"address": "pump-1", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )

    assert decision.requested_mode == "live"
    assert decision.effective_mode == "shadow"
    assert decision.effective_execution_state == "shadow"
    assert decision.action == "shadow"
    assert decision.reason == "scorecard_negative"


def test_stale_scorecard_is_ignored(tmp_path: Path, monkeypatch) -> None:
    now = dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc)
    scorecard_path = tmp_path / "research_scorecard.json"
    _write_scorecard(
        scorecard_path,
        generated_at=now - dt.timedelta(hours=8),
        group="pump_early",
        count=24,
        avg_pnl_pct=-8.5,
        win_rate_pct=25.0,
    )

    monkeypatch.setattr(strategy_runtime, "CFG", _make_cfg(STRATEGY_SCORECARD_MAX_AGE_MIN=60.0))
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", scorecard_path)
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    decision = strategy_runtime.evaluate_candidate(
        {"address": "pump-2", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )

    assert decision.requested_mode == "live"
    assert decision.effective_mode == "live"
    assert decision.effective_execution_state == "live"
    assert decision.action == "live"
    assert decision.reason == "confirm_ok"


def test_three_loss_streak_demotes_live_pump_to_shadow(monkeypatch, tmp_path: Path) -> None:
    now = dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(strategy_runtime, "CFG", _make_cfg())
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", tmp_path / "missing_scorecard.json")
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    strategy_runtime.record_trade_close("pump_early", -8.0, exit_reason="STOP_LOSS", execution_state="live")
    strategy_runtime.record_trade_close("pump_early", -6.0, exit_reason="EARLY_DROP", execution_state="live")
    strategy_runtime.record_trade_close("pump_early", -5.0, exit_reason="NO_PUMP_EXIT", execution_state="live")

    decision = strategy_runtime.evaluate_candidate(
        {"address": "pump-3", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )

    assert decision.effective_mode == "shadow"
    assert decision.effective_execution_state == "shadow"
    assert decision.action == "shadow"
    assert "loss_streak" in decision.reason
    assert decision.health_state == "cooldown"


def test_sniper_mode_demotes_after_four_loss_streak(monkeypatch, tmp_path: Path) -> None:
    now = dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(
        strategy_runtime,
        "CFG",
        _make_cfg(PUMP_EARLY_SNIPER_ENABLED=True, DRY_RUN=False),
    )
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", tmp_path / "missing_scorecard.json")
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    for pnl in (-2.0, -3.0, -1.0):
        strategy_runtime.record_trade_close("pump_early", pnl, exit_reason="STOP_LOSS", execution_state="live")

    before = strategy_runtime.evaluate_candidate(
        {"address": "pump-sniper-before", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )
    assert before.effective_mode == "live"

    strategy_runtime.record_trade_close("pump_early", -4.0, exit_reason="STOP_LOSS", execution_state="live")
    after = strategy_runtime.evaluate_candidate(
        {"address": "pump-sniper-after", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )

    assert after.effective_mode == "shadow"
    assert after.effective_execution_state == "shadow"
    assert "loss_streak" in after.reason


def test_sniper_paper_continues_after_health_demote(monkeypatch, tmp_path: Path) -> None:
    now = dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(
        strategy_runtime,
        "CFG",
        _make_cfg(PUMP_EARLY_SNIPER_ENABLED=True, DRY_RUN=True, PAPER_PNL_STRICT_HEALTH=False),
    )
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", tmp_path / "missing_scorecard.json")
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    for pnl in (-2.0, -3.0, -1.0, -4.0):
        strategy_runtime.record_trade_close("pump_early", pnl, exit_reason="STOP_LOSS", execution_state="live")

    decision = strategy_runtime.evaluate_candidate(
        {"address": "pump-paper-after", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )

    assert decision.effective_mode == "live"
    assert decision.effective_execution_state == "paper_recovery"
    assert decision.action == "live"
    assert decision.size_cap_multiplier == 0.20
    assert "paper_" in decision.reason


def test_strict_paper_health_demotes_productive_lane_to_shadow(monkeypatch, tmp_path: Path) -> None:
    now = dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(
        strategy_runtime,
        "CFG",
        _make_cfg(PUMP_EARLY_SNIPER_ENABLED=True, DRY_RUN=True, PAPER_PNL_STRICT_HEALTH=True),
    )
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", tmp_path / "missing_scorecard.json")
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    for pnl in (-2.0, -3.0, -1.0, -4.0):
        strategy_runtime.record_trade_close("pump_early", pnl, exit_reason="STOP_LOSS", execution_state="live")

    decision = strategy_runtime.evaluate_candidate(
        {
            "address": "pump-paper-strict",
            "entry_lane": "pump_early_pumpswap_profit",
            "age_min": 6.0,
            "liquidity_usd": 12_000.0,
        },
        regime="pump_early",
        has_route=True,
        now=now,
    )

    assert decision.effective_mode == "shadow"
    assert decision.effective_execution_state == "shadow"
    assert decision.action == "shadow"
    assert "loss_streak" in decision.reason


def test_sniper_fast_confirm_uses_single_snapshot(monkeypatch, tmp_path: Path) -> None:
    now = dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(
        strategy_runtime,
        "CFG",
        _make_cfg(
            PUMP_EARLY_SNIPER_ENABLED=True,
            DRY_RUN=True,
            PUMP_EARLY_CONFIRM_SNAPSHOTS=2,
            STRATEGY_CONFIRM_REQUIRE_ROUTE=True,
        ),
    )
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", tmp_path / "missing_scorecard.json")
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    decision = strategy_runtime.evaluate_candidate(
        {
            "address": "pump-fast",
            "entry_regime": "pump_early",
            "age_min": 6.0,
            "liquidity_usd": 12_000.0,
            "has_jupiter_route": 1,
            "txns_last_5m": 45,
            "price_pct_5m": 12.0,
        },
        regime="pump_early",
        has_route=True,
        now=now,
    )

    assert decision.action == "live"
    assert decision.confirmations_required == 1
    assert decision.reason == "confirm_ok"


def test_cooldown_requires_ready_recovery_signal(monkeypatch, tmp_path: Path) -> None:
    now = dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc)
    scorecard_path = tmp_path / "research_scorecard.json"
    _write_scorecard(scorecard_path, generated_at=now, group="pump_early", count=80, avg_pnl_pct=4.0, win_rate_pct=45.0)

    monkeypatch.setattr(strategy_runtime, "CFG", _make_cfg())
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", scorecard_path)
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    strategy_runtime.record_trade_close("pump_early", -8.0, exit_reason="STOP_LOSS", execution_state="live")
    strategy_runtime.record_trade_close("pump_early", -6.0, exit_reason="EARLY_DROP", execution_state="live")
    strategy_runtime.record_trade_close("pump_early", -5.0, exit_reason="NO_PUMP_EXIT", execution_state="live")
    strategy_runtime.evaluate_candidate(
        {"address": "pump-4a", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )
    health = strategy_runtime._HEALTH["pump_early"]
    health.cooldown_until = now - dt.timedelta(minutes=1)

    decision = strategy_runtime.evaluate_candidate(
        {"address": "pump-4", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )

    assert decision.effective_mode == "shadow"
    assert decision.effective_execution_state == "shadow"
    assert decision.action == "shadow"
    assert decision.reason == "recovery_not_ready"
    assert decision.health_state == "shadow_wait"


def test_ready_recovery_signal_enables_live_canary(monkeypatch, tmp_path: Path) -> None:
    now = dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc)
    scorecard_path = tmp_path / "research_scorecard.json"
    _write_scorecard(scorecard_path, generated_at=now, group="pump_early", count=80, avg_pnl_pct=4.0, win_rate_pct=45.0)

    monkeypatch.setattr(strategy_runtime, "CFG", _make_cfg())
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", scorecard_path)
    monkeypatch.setattr(
        strategy_runtime.research_runtime,
        "load_live_rank_gate",
        lambda *args, **kwargs: _rank_gate(ready=True, source="research_thresholds", selected=25, avg_realized=4.0),
    )
    _reset_strategy_state()

    strategy_runtime.record_trade_close("pump_early", -8.0, exit_reason="STOP_LOSS", execution_state="live")
    strategy_runtime.record_trade_close("pump_early", -6.0, exit_reason="EARLY_DROP", execution_state="live")
    strategy_runtime.record_trade_close("pump_early", -5.0, exit_reason="NO_PUMP_EXIT", execution_state="live")
    strategy_runtime.evaluate_candidate(
        {"address": "pump-5a", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )
    health = strategy_runtime._HEALTH["pump_early"]
    health.cooldown_until = now - dt.timedelta(minutes=1)

    decision = strategy_runtime.evaluate_candidate(
        {"address": "pump-5", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )

    assert decision.effective_mode == "live"
    assert decision.effective_execution_state == "recovery"
    assert decision.action == "live"
    assert decision.health_state == "recovery"
    assert decision.size_cap_multiplier == 0.10


def test_minimally_relaxed_recovery_win_rate_enables_live_canary(monkeypatch, tmp_path: Path) -> None:
    now = dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc)
    scorecard_path = tmp_path / "research_scorecard.json"
    _write_scorecard(scorecard_path, generated_at=now, group="pump_early", count=80, avg_pnl_pct=4.0, win_rate_pct=41.71)

    monkeypatch.setattr(
        strategy_runtime,
        "CFG",
        _make_cfg(PUMP_EARLY_RECOVERY_MIN_WIN_RATE_PCT=41.5),
    )
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", scorecard_path)
    monkeypatch.setattr(
        strategy_runtime.research_runtime,
        "load_live_rank_gate",
        lambda *args, **kwargs: _rank_gate(ready=True, source="research_thresholds", selected=25, avg_realized=4.0),
    )
    _reset_strategy_state()

    strategy_runtime.record_trade_close("pump_early", -8.0, exit_reason="STOP_LOSS", execution_state="live")
    strategy_runtime.record_trade_close("pump_early", -6.0, exit_reason="EARLY_DROP", execution_state="live")
    strategy_runtime.record_trade_close("pump_early", -5.0, exit_reason="NO_PUMP_EXIT", execution_state="live")
    strategy_runtime.evaluate_candidate(
        {"address": "pump-5b-a", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )
    health = strategy_runtime._HEALTH["pump_early"]
    health.cooldown_until = now - dt.timedelta(minutes=1)

    decision = strategy_runtime.evaluate_candidate(
        {"address": "pump-5b", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )

    assert decision.effective_mode == "live"
    assert decision.effective_execution_state == "recovery"
    assert decision.action == "live"
    assert decision.health_state == "recovery"
    assert decision.size_cap_multiplier == 0.10


def test_recovery_promotes_back_to_normal_after_10_good_canary_closes(monkeypatch, tmp_path: Path) -> None:
    now = dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(strategy_runtime, "CFG", _make_cfg())
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", tmp_path / "missing_scorecard.json")
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    health = strategy_runtime._HEALTH["pump_early"]
    health.recovery_armed = True
    health.canary_active = True
    for pnl in (1.2, 1.0, 1.5, 0.8, 1.1, 1.4, 1.3, 1.0, 1.6, 1.2):
        strategy_runtime.record_trade_close("pump_early", pnl, exit_reason="POST_PARTIAL_STOP", execution_state="recovery")

    decision = strategy_runtime.evaluate_candidate(
        {"address": "pump-6", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )

    assert decision.effective_mode == "live"
    assert decision.effective_execution_state == "live"
    assert decision.action == "live"
    assert decision.reason == "confirm_ok"
    assert decision.health_state == "normal"


def test_bucket_health_blocks_toxic_profit_bucket_without_global_shutdown(monkeypatch, tmp_path: Path) -> None:
    now = dt.datetime(2026, 4, 6, 13, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(strategy_runtime, "CFG", _make_cfg())
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", tmp_path / "missing_scorecard.json")
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    for pnl in (-2.0, -3.0, -4.0, -5.0):
        strategy_runtime.record_trade_close(
            "pump_early",
            pnl,
            exit_reason="STOP_LOSS",
            execution_state="live",
            entry_lane="pump_early_pumpswap_profit",
            dex_id="pumpswap",
            liquidity_proxy_flag=False,
            mcap_bucket="<25k",
            price5m_bucket="25_50",
            gate_profile="pumpswap_profit_broad",
        )
        strategy_runtime.record_trade_close(
            "pump_early",
            2.0,
            exit_reason="POST_PARTIAL_STOP",
            execution_state="live",
            entry_lane="pump_early_pumpswap_profit",
            dex_id="pumpswap",
            liquidity_proxy_flag=False,
            mcap_bucket="<25k",
            price5m_bucket="100_180",
            gate_profile="pumpswap_profit_broad",
        )

    blocked = strategy_runtime.evaluate_candidate(
        {
            "address": "bucket-blocked",
            "entry_lane": "pump_early_pumpswap_profit",
            "gate_profile": "pumpswap_profit_broad",
            "dex_id": "pumpswap",
            "liquidity_is_proxy": 0,
            "mcap_bucket": "<25k",
            "price5m_bucket": "25_50",
            "age_min": 6.0,
        },
        regime="pump_early",
        has_route=True,
        now=now,
    )
    other_bucket = strategy_runtime.evaluate_candidate(
        {
            "address": "bucket-open",
            "entry_lane": "pump_early_pumpswap_profit",
            "gate_profile": "pumpswap_profit_broad",
            "dex_id": "pumpswap",
            "liquidity_is_proxy": 0,
            "mcap_bucket": "<25k",
            "price5m_bucket": "25_50_alt",
            "age_min": 6.0,
        },
        regime="pump_early",
        has_route=True,
        now=now,
    )

    assert blocked.effective_mode == "shadow"
    assert "bucket:" in blocked.reason
    assert other_bucket.effective_mode == "live"


def test_severe_exit_count_includes_stop_early_adverse_and_large_loss(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(strategy_runtime, "CFG", _make_cfg())
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", tmp_path / "missing_scorecard.json")
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    strategy_runtime.record_trade_close("pump_early", -8.0, exit_reason="STOP_LOSS", execution_state="live")
    strategy_runtime.record_trade_close("pump_early", -5.0, exit_reason="EARLY_DROP", execution_state="live")
    strategy_runtime.record_trade_close("pump_early", -9.0, exit_reason="ADVERSE_TICK", execution_state="live")
    strategy_runtime.record_trade_close("pump_early", -26.0, exit_reason="NO_PUMP_EXIT", execution_state="live")

    health = strategy_runtime.describe_regime_health()["pump_early"]

    assert health["severe_exit_count"] == 4


def test_recent_productive_window_recovers_without_rank_gate_ready(monkeypatch, tmp_path: Path) -> None:
    now = dt.datetime(2026, 4, 20, 16, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(
        strategy_runtime,
        "CFG",
        _make_cfg(
            PUMP_EARLY_SNIPER_ENABLED=True,
            DRY_RUN=False,
            PUMP_EARLY_PROFIT_RECOVERY_RECENT_TRADES=8,
            PUMP_EARLY_PROFIT_RECOVERY_RECENT_MIN_AVG_PNL_PCT=5.0,
            PUMP_EARLY_PROFIT_RECOVERY_RECENT_MAX_CONSECUTIVE_LOSSES=2,
        ),
    )
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", tmp_path / "missing_scorecard.json")
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    for pnl in (-8.0, -6.0, -5.0, -4.0):
        strategy_runtime.record_trade_close("pump_early", pnl, exit_reason="STOP_LOSS", execution_state="live")

    first = strategy_runtime.evaluate_candidate(
        {"address": "pump-recover-first", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now,
    )
    assert first.effective_mode == "shadow"

    health = strategy_runtime._HEALTH["pump_early"]
    health.cooldown_until = now - dt.timedelta(minutes=1)

    for pnl in (9.0, 11.0, 7.0, 6.0, 8.0, -1.0, 10.0, 12.0):
        strategy_runtime.record_trade_close("pump_early", pnl, exit_reason="POST_PARTIAL_TRAILING", execution_state="live")

    second = strategy_runtime.evaluate_candidate(
        {"address": "pump-recover-second", "age_min": 6.0, "liquidity_usd": 12_000.0},
        regime="pump_early",
        has_route=True,
        now=now + dt.timedelta(minutes=1),
    )
    regime_health = strategy_runtime.describe_regime_health(now + dt.timedelta(minutes=1))["pump_early"]

    assert second.effective_mode == "live"
    assert second.effective_execution_state == "recovery"
    assert regime_health["recovery_ready"] is True
    assert regime_health["recovery_basis"]["source"] == "recent_clean_productive_window"


def test_shadow_productive_recovery_unsticks_recovery_not_ready(monkeypatch, tmp_path: Path) -> None:
    now = dt.datetime(2026, 4, 25, 9, 0, tzinfo=dt.timezone.utc)
    shadow_events = tmp_path / "candidate_outcomes.jsonl"
    _write_shadow_recovery_events(
        shadow_events,
        now=now,
        pnls=[26.8, -17.8, -40.8, -20.1, 3.7, 66.4, -33.4, 74.4],
        exits=[
            "POST_PARTIAL_TRAILING",
            "PRE_PARTIAL_TIME_STOP",
            "LIQUIDITY_CRUSH",
            "PRE_PARTIAL_TIME_STOP",
            "POST_PARTIAL_STOP",
            "POST_PARTIAL_TRAILING",
            "NO_PUMP_EXIT",
            "POST_PARTIAL_TRAILING",
        ],
    )
    with shadow_events.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "ts_utc": (now + dt.timedelta(seconds=1)).isoformat(),
                    "event_type": "candidate_outcome",
                    "address": "shadow-non-product",
                    "regime": "pump_early",
                    "source": "research_shadow",
                    "shadow_kind": "execution",
                    "reason": "strategy:recovery_not_ready",
                    "pnl_pct": -80.0,
                    "exit_reason": "LIQUIDITY_CRUSH",
                    "venue_is_pumpswap": 0,
                    "liquidity_usd": 2_300.0,
                    "market_cap_usd": 2_600_000.0,
                    "age_minutes": 12.0,
                    "score_total": 45,
                    "price5m_bucket": "0_25",
                }
            )
            + "\n"
        )
    monkeypatch.setattr(
        strategy_runtime,
        "CFG",
        _make_cfg(
            PUMP_EARLY_SNIPER_ENABLED=True,
            DRY_RUN=True,
            PAPER_PNL_STRICT_HEALTH=True,
            PUMP_EARLY_SHADOW_RECOVERY_ENABLED=True,
            PUMP_EARLY_SHADOW_RECOVERY_MIN_TRADES=8,
            PUMP_EARLY_SHADOW_RECOVERY_MIN_AVG_PNL_PCT=5.0,
            PUMP_EARLY_SHADOW_RECOVERY_MIN_WIN_RATE_PCT=45.0,
            PUMP_EARLY_SHADOW_RECOVERY_MAX_SEVERE_EXITS=2,
            PUMP_EARLY_SHADOW_RECOVERY_MAX_LIQ_CRUSH=1,
        ),
    )
    monkeypatch.setattr(strategy_runtime, "_SCORECARD_PATH", tmp_path / "missing_scorecard.json")
    monkeypatch.setattr(strategy_runtime, "_SHADOW_RECOVERY_EVENTS_PATH", shadow_events)
    monkeypatch.setattr(strategy_runtime.research_runtime, "load_live_rank_gate", lambda *args, **kwargs: _rank_gate())
    _reset_strategy_state()

    health = strategy_runtime._HEALTH["pump_early"]
    health.recovery_armed = True
    health.cooldown_until = now - dt.timedelta(minutes=1)

    decision = strategy_runtime.evaluate_candidate(
        {
            "address": "pump-shadow-recovered",
            "entry_lane": "pump_early_pumpswap_profit",
            "gate_profile": "pumpswap_profit_broad",
            "dex_id": "pumpswap",
            "age_min": 6.0,
            "liquidity_usd": 12_000.0,
        },
        regime="pump_early",
        has_route=True,
        now=now,
    )
    regime_health = strategy_runtime.describe_regime_health(now)["pump_early"]

    assert decision.effective_mode == "live"
    assert decision.effective_execution_state == "recovery"
    assert decision.action == "live"
    assert decision.health_state == "recovery"
    assert regime_health["recovery_ready"] is True
    assert regime_health["recovery_basis"]["source"] == "shadow_productive_recovery"
    assert regime_health["current_gate_rebased"] is True
