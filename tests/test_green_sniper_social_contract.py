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
        "GREEN_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS": 6,
        "GREEN_SNIPER_LIVE_ENABLED": True,
        "GREEN_SNIPER_REQUIRE_ROUTE_LIVE": True,
        "GREEN_SNIPER_LIVE_MIN_AGE_MIN": 0.35,
        "GREEN_SNIPER_LIVE_MAX_AGE_MIN": 6.0,
        "GREEN_SNIPER_LIVE_MIN_LIQUIDITY_USD": 2500.0,
        "GREEN_SNIPER_LIVE_MAX_PRICE_IMPACT_PCT": 12.0,
        "GREEN_SNIPER_LIVE_MIN_TXNS_5M": 60,
        "GREEN_SNIPER_REQUIRE_SOCIALS": False,
        "GREEN_SNIPER_SOCIALS_BONUS_ENABLED": True,
        "GREEN_SNIPER_SOCIALS_SCORE_BONUS": 5.0,
        "GREEN_SNIPER_SOCIALS_RISK_PENALTY": 5.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_green_sniper_no_socials_still_can_buy(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg())
    token = _load("newborn_green_80pct.json")
    token.pop("social_ok", None)
    token["social_status"] = "unknown"

    decision = gate.evaluate_green_sniper(token, dry_run=True, live=False)

    assert decision.action == "buy"
    assert "social" not in ",".join(decision.reject_reasons)


def test_green_sniper_socials_false_still_can_buy(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg())
    token = _load("newborn_green_80pct.json")
    token["social_ok"] = False
    token["social_status"] = "missing"

    decision = gate.evaluate_green_sniper(token, dry_run=True, live=False)

    assert decision.action == "buy"
    assert "social" not in ",".join(decision.reject_reasons)


def test_socials_bonus_can_increase_score_but_not_required(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg())
    base_token = _load("newborn_green_80pct.json")
    social_token = dict(base_token)
    social_token.update(
        {
            "social_status": "present",
            "social_ok": True,
            "twitter_present": 1,
            "social_link_count": 1,
        }
    )

    base = gate.evaluate_green_sniper(base_token, dry_run=True, live=False)
    social = gate.evaluate_green_sniper(social_token, dry_run=True, live=False)

    assert base.action == "buy"
    assert social.action == "buy"
    assert social.score > base.score


def test_suspicious_socials_do_not_hard_reject(monkeypatch) -> None:
    monkeypatch.setattr(gate, "CFG", _cfg())
    token = _load("newborn_green_80pct.json")
    token.update(
        {
            "social_status": "suspicious",
            "social_ok": True,
            "social_link_count": 1,
            "social_risk_flags": "reused_link",
        }
    )

    decision = gate.evaluate_green_sniper(token, dry_run=True, live=False)

    assert decision.action == "buy"
    assert "reused_link" in decision.social_risk_flags
    assert "social" not in ",".join(decision.reject_reasons)
