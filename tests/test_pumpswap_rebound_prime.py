from __future__ import annotations

import json
from pathlib import Path

from analytics.pumpswap_rebound_prime import (
    GATE_PUMPSWAP_REBOUND_PRIME,
    LANE_PUMPSWAP_REBOUND_PRIME,
    apply_pumpswap_rebound_prime_context,
    evaluate_pumpswap_rebound_prime,
    write_pumpswap_rebound_prime_report,
)
from test_pump_live_floor import _load_entry_quality_gate


def _rebound_token(**overrides: object) -> dict[str, object]:
    token: dict[str, object] = {
        "dex_id": "pumpswap",
        "price_usd": 0.00001,
        "price_pct_5m": -30.0,
        "txns_last_5m": 650,
        "liquidity_usd": 12_000.0,
        "market_cap_usd": 20_000.0,
        "liquidity_usd_is_proxy": 0,
        "has_jupiter_route": 1,
        "price_impact_pct": 6.0,
        "total_pnl_pct": 20.0,
    }
    token.update(overrides)
    return token


def test_rebound_prime_allows_supported_pumpswap_rebound() -> None:
    decision = evaluate_pumpswap_rebound_prime(_rebound_token())

    assert decision.allowed is True
    assert decision.failures == ()


def test_rebound_context_sets_own_lane() -> None:
    token = _rebound_token()

    apply_pumpswap_rebound_prime_context(token)

    assert token["entry_lane"] == LANE_PUMPSWAP_REBOUND_PRIME
    assert token["gate_profile"] == GATE_PUMPSWAP_REBOUND_PRIME
    assert token["profit_lane_tier"] == LANE_PUMPSWAP_REBOUND_PRIME


def test_rebound_prime_rejects_bad_inputs() -> None:
    token = _rebound_token(
        dex_id="pumpfun",
        price_pct_5m=-10,
        txns_last_5m=120,
        liquidity_usd=5_000,
        market_cap_usd=70_000,
        liquidity_usd_is_proxy=1,
        has_jupiter_route=0,
        price_impact_pct=20,
    )

    decision = evaluate_pumpswap_rebound_prime(token)

    assert decision.allowed is False
    assert "dex!=pumpswap" in decision.failures
    assert "price5m>-25" in decision.failures
    assert "txns5m<500" in decision.failures
    assert "liq<10000" in decision.failures
    assert "mcap>50000" in decision.failures
    assert "proxy_liquidity" in decision.failures
    assert "route_required" in decision.failures
    assert "impact>12" in decision.failures


def test_rebound_prime_runtime_lane_precedes_deep_negative_shape_block() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = _rebound_token(age_min=7.0, score_total=45, volume_24h_usd=120_000.0)

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 40.0})

    assert ok is True
    assert reason == ""
    assert token["entry_lane"] == LANE_PUMPSWAP_REBOUND_PRIME
    assert token["gate_profile"] == GATE_PUMPSWAP_REBOUND_PRIME
    assert token["profit_lane_tier"] == LANE_PUMPSWAP_REBOUND_PRIME
    assert token["profit_shape_guard_failures"] == ""


def test_rebound_prime_report_outputs_json_and_markdown(tmp_path: Path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        _rebound_token(address="A", total_pnl_pct=40, max_pnl_pct_seen=150),
        _rebound_token(address="B", txns_last_5m=100, total_pnl_pct=-20),
    ]
    (metrics / "candidate_outcomes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    report = write_pumpswap_rebound_prime_report(tmp_path)

    assert report["candidates"]["count"] == 1
    assert report["candidates"]["runner_100_count"] == 1
    assert (metrics / "pumpswap_rebound_prime_report.json").exists()
    assert (tmp_path / "docs" / "PUMPSWAP_REBOUND_PRIME.md").exists()


def test_env_files_have_rebound_flags_once() -> None:
    required = {
        "PUMPSWAP_REBOUND_PRIME_ENABLED",
        "PUMPSWAP_REBOUND_PRIME_MAX_PRICE5M",
        "PUMPSWAP_REBOUND_PRIME_MIN_TXNS_5M",
        "PUMPSWAP_REBOUND_PRIME_MIN_LIQUIDITY_USD",
        "PUMPSWAP_REBOUND_PRIME_MIN_MCAP_USD",
        "PUMPSWAP_REBOUND_PRIME_MAX_MCAP_USD",
        "PUMPSWAP_REBOUND_PRIME_REQUIRE_REAL_LIQUIDITY",
        "PUMPSWAP_REBOUND_PRIME_REQUIRE_ROUTE",
        "PUMPSWAP_REBOUND_PRIME_MAX_PRICE_IMPACT_PCT",
    }
    for name in (".env", ".env.example"):
        keys = []
        for line in Path(name).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                keys.append(stripped.split("=", 1)[0].strip())
        assert len(keys) == len(set(keys))
        assert required <= set(keys)
