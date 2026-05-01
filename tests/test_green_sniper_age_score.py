from __future__ import annotations

import datetime as dt

from analytics.green_sniper_gate import evaluate_green_sniper
from analytics.green_sniper_score import score_green_sniper
from analytics.token_time import compute_age_minutes


def _token(created_key: str = "created_at") -> dict:
    created = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    return {
        "address": "AGE",
        created_key: created.isoformat(),
        "price_pct_5m": 80,
        "txns_last_5m": 120,
        "txns_last_5m_buys": 90,
        "txns_last_5m_sells": 30,
        "liquidity_usd": 6000,
        "market_cap_usd": 30000,
        "price_impact_pct": 4,
        "has_jupiter_route": 1,
        "rank_score": 70,
        "price_usd": 0.00001,
    }


def test_score_uses_created_at_age_when_age_minutes_missing() -> None:
    score = score_green_sniper(_token(), has_route=True, proxy_liquidity=False, live=False)
    assert score.age_component == -8.0


def test_gate_uses_created_at_age_when_age_minutes_missing() -> None:
    decision = evaluate_green_sniper(_token("createdAt"), dry_run=True, live=False)
    assert "too_old" in decision.reject_reasons


def test_compute_age_minutes_handles_epoch_ms() -> None:
    now = dt.datetime(2026, 5, 1, 10, 0, tzinfo=dt.timezone.utc)
    token = {"created_at": int((now - dt.timedelta(minutes=3)).timestamp() * 1000)}
    assert 2.9 <= compute_age_minutes(token, now=now) <= 3.1
