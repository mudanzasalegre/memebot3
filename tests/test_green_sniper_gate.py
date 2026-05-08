from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import analytics.green_sniper_gate as gate


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sniper"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _cfg(**overrides):
    base = {
        "GREEN_SNIPER_ENABLED": True,
        "GREEN_SNIPER_MIN_AGE_MIN": 0.15,
        "GREEN_SNIPER_MAX_AGE_MIN": 8.0,
        "GREEN_SNIPER_MIN_LIQUIDITY_USD": 1200.0,
        "GREEN_SNIPER_MIN_MARKET_CAP_USD": 2000.0,
        "GREEN_SNIPER_MAX_MARKET_CAP_USD": 180000.0,
        "GREEN_SNIPER_MIN_PRICE_PCT_5M": 20.0,
        "GREEN_SNIPER_MAX_PRICE_PCT_5M": 280.0,
        "GREEN_SNIPER_MIN_TXNS_5M": 35,
        "GREEN_SNIPER_HOT_MIN_TXNS_5M": 80,
        "GREEN_SNIPER_MIN_BUY_SELL_RATIO": 1.15,
        "GREEN_SNIPER_MAX_PRICE_IMPACT_PCT": 20.0,
        "GREEN_SNIPER_ALLOW_PROXY_LIQUIDITY_PAPER": True,
        "GREEN_SNIPER_REQUIRE_ROUTE_PAPER": False,
        "PAPER_SNIPER_MODE": True,
        "GREEN_SNIPER_PAPER_BIRTH_PROBE_ENABLED": True,
        "GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_AGE_MIN": 3.0,
        "GREEN_SNIPER_PAPER_BIRTH_PROBE_MIN_LIQUIDITY_USD": 1000.0,
        "GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_PRICE_IMPACT_PCT": 25.0,
        "GREEN_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS": 6,
        "GREEN_SNIPER_LIVE_ENABLED": True,
        "GREEN_SNIPER_REQUIRE_ROUTE_LIVE": True,
        "GREEN_SNIPER_LIVE_MIN_AGE_MIN": 0.35,
        "GREEN_SNIPER_LIVE_MAX_AGE_MIN": 6.0,
        "GREEN_SNIPER_LIVE_MIN_LIQUIDITY_USD": 2500.0,
        "GREEN_SNIPER_LIVE_MAX_PRICE_IMPACT_PCT": 12.0,
        "GREEN_SNIPER_LIVE_MIN_TXNS_5M": 60,
        "GREEN_SNIPER_POLICY_MODE": "shadow",
        "GREEN_SNIPER_BUY_RESTRICTED_ENABLED": True,
        "GREEN_SNIPER_RESTRICTED_MIN_RANK": 64.0,
        "GREEN_SNIPER_RESTRICTED_MIN_TXNS": 300,
        "GREEN_SNIPER_RESTRICTED_MIN_LIQUIDITY": 10000.0,
        "GREEN_SNIPER_RESTRICTED_MIN_MCAP": 25000.0,
        "GREEN_SNIPER_RESTRICTED_MAX_MCAP": 100000.0,
        "GREEN_SNIPER_RESTRICTED_MIN_PRICE5M": 25.0,
        "GREEN_SNIPER_RESTRICTED_MAX_PRICE5M": 100.0,
        "GREEN_SNIPER_RESTRICTED_REQUIRE_ROUTE": True,
        "GREEN_SNIPER_RESTRICTED_MAX_PRICE_IMPACT_PCT": 12.0,
        "GREEN_SNIPER_RESTRICTED_REQUIRE_PROVIDER_HEALTH": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_hot_green_token_passes(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg())
    decision = gate.evaluate_green_sniper(_load("newborn_green_80pct.json"), dry_run=True, live=False)
    assert decision.action == "shadow"
    assert decision.lane == "pump_early_green_candle_sniper"
    assert decision.policy_category == "green_sniper_shadow"


def test_restricted_green_token_can_buy(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg())
    token = _load("newborn_green_80pct.json")
    token.update({"rank_score": 70, "txns_last_5m": 350, "liquidity_usd": 15_000})
    decision = gate.evaluate_green_sniper(token, dry_run=True, live=False)
    assert decision.action == "buy"
    assert decision.gate_profile == "green_sniper_restricted_buy"
    assert decision.policy_category == "green_sniper_restricted_buy"


def test_late_500_pct_rejects(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg())
    decision = gate.evaluate_green_sniper(_load("late_green_500pct.json"), dry_run=True, live=False)
    assert decision.action == "shadow"
    assert decision.gate_profile == "late_momentum_watch"
    assert "mcap_out_of_band" in decision.reject_reasons


def test_no_route_paper_can_buy(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg(GREEN_SNIPER_REQUIRE_ROUTE_PAPER=False))
    decision = gate.evaluate_green_sniper(_load("no_route_paper_ok.json"), dry_run=True, live=False)
    assert decision.action == "shadow"
    assert decision.reason == "green_sniper_policy_shadow"


def test_queue_age_is_used_when_age_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg())
    token = _load("newborn_green_80pct.json")
    token.pop("age_minutes", None)
    token.pop("age_min", None)
    token["queue_age_minutes"] = 1.0

    decision = gate.evaluate_green_sniper(token, dry_run=True, live=False)

    assert decision.action == "shadow"
    assert "too_young" not in decision.reject_reasons


def test_no_route_live_rejects(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg(GREEN_SNIPER_REQUIRE_ROUTE_LIVE=True))
    decision = gate.evaluate_green_sniper(_load("no_route_live_reject.json"), dry_run=False, live=True)
    assert decision.action in {"shadow", "reject", "delay"}
    assert "no_route" in decision.reject_reasons


def test_proxy_liquidity_live_rejects(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg())
    decision = gate.evaluate_green_sniper(_load("liquidity_proxy_live_reject.json"), dry_run=False, live=True)
    assert decision.action == "reject"
    assert "proxy_liquidity_live" in decision.reject_reasons


def test_proxy_liquidity_paper_goes_to_shadow_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        gate,
        "CFG",
        _cfg(GREEN_SNIPER_ALLOW_PROXY_LIQUIDITY_PAPER=False, GREEN_SNIPER_PAPER_BIRTH_PROBE_ENABLED=False),
    )
    decision = gate.evaluate_green_sniper(_load("liquidity_proxy_live_reject.json"), dry_run=True, live=False)

    assert decision.action == "shadow"
    assert "proxy_liquidity_paper_disabled" in decision.reject_reasons


def test_proxy_liquidity_paper_is_shadow_not_productive_buy(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg(GREEN_SNIPER_ALLOW_PROXY_LIQUIDITY_PAPER=True))
    decision = gate.evaluate_green_sniper(_load("liquidity_proxy_live_reject.json"), dry_run=True, live=False)

    assert decision.action == "shadow"
    assert "proxy_liquidity_productive_block" in decision.reject_reasons


def test_paper_birth_probe_is_shadow_first(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg(GREEN_SNIPER_ALLOW_PROXY_LIQUIDITY_PAPER=False))
    token = {
        "address": "Probe111111111111111111111111111111111111pump",
        "discovered_via": "pumpfun",
        "age_minutes": 0.4,
        "liquidity_usd": 1200.0,
        "liquidity_is_proxy": 1,
        "price_impact_pct": 0.0,
    }

    decision = gate.evaluate_green_sniper(token, dry_run=True, live=False)

    assert decision.action == "shadow"
    assert decision.paper_birth_probe is True
    assert decision.size_hint == "micro"
    assert "paper_birth_probe" in decision.reason
