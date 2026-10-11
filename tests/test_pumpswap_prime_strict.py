from __future__ import annotations

import json
from pathlib import Path
import pytest

from analytics.pumpswap_prime_strict import (
    build_pumpswap_prime_strict_report,
    evaluate_pumpswap_prime_strict,
    write_pumpswap_prime_strict_report,
)
from test_pump_live_floor import _load_entry_quality_gate


def _prime_token(**overrides: object) -> dict[str, object]:
    token: dict[str, object] = {
        "entry_lane": "pump_early_pumpswap_profit",
        "gate_profile": "pumpswap_profit_prime",
        "dex_id": "pumpswap",
        "price_usd": 0.00001,
        "txns_last_5m": 650,
        "liquidity_usd": 12_000.0,
        "liquidity_usd_is_proxy": 0,
        "has_jupiter_route": 1,
        "price_impact_pct": 6.0,
        "total_pnl_pct": 12.0,
    }
    token.update(overrides)
    return token


def test_pumpswap_prime_strict_passes_clean_prime() -> None:
    decision = evaluate_pumpswap_prime_strict(_prime_token())

    assert decision.allowed is True
    assert decision.failures == ()


def test_pumpswap_prime_strict_reports_each_failure() -> None:
    token = _prime_token(
        txns_last_5m=100,
        liquidity_usd=9_000.0,
        liquidity_usd_is_proxy=1,
        has_jupiter_route=0,
        price_impact_pct=18.0,
    )

    decision = evaluate_pumpswap_prime_strict(token)

    assert decision.allowed is False
    assert "txns5m<500" in decision.failures
    assert "liq<10000" in decision.failures
    assert "proxy_liquidity" in decision.failures
    assert "route_required" in decision.failures
    assert "impact>12" in decision.failures
    assert decision.block_reason.startswith("pumpswap_prime_strict_failed:")


def test_prime_strict_failure_demotes_to_research_shadow_reason() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = _prime_token(
        age_min=6.0,
        score_total=40,
        market_cap_usd=18_000.0,
        volume_24h_usd=120_000.0,
        price_pct_5m=7.0,
        txns_last_5m=300,
        liquidity_usd=8_500.0,
    )

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 40.0})

    assert ok is False
    assert token["entry_lane"] == "pump_early_sniper_research"
    assert token["gate_profile"] == "pumpswap_profit_research"
    assert token["profit_lane_tier"] == "pumpswap_prime_strict_blocked"
    assert "pumpswap_prime_strict_failed:" in reason


def test_prime_strict_does_not_block_research_rank_canary() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = _prime_token(
        age_min=6.0,
        score_total=40,
        market_cap_usd=18_000.0,
        volume_24h_usd=120_000.0,
        price_pct_5m=7.0,
        txns_last_5m=300,
        liquidity_usd=8_500.0,
    )

    ok, _reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 70.0})

    assert ok is False
    assert token["entry_lane"] == "pump_early_sniper_research"
    assert token["gate_profile"] == "pumpswap_profit_research"


def test_prime_strict_report_outputs_json_and_markdown(tmp_path: Path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        _prime_token(address="A", total_pnl_pct=50, max_pnl_pct_seen=120),
        _prime_token(address="B", txns_last_5m=200, total_pnl_pct=-40, exit_reason="ADVERSE_TICK"),
    ]
    (metrics / "candidate_outcomes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    report = write_pumpswap_prime_strict_report(tmp_path)

    assert report["previous_prime"]["count"] == 2
    assert report["strict_passed"]["count"] == 1
    assert report["strict_blocked"]["adverse_tick_count"] == 1
    assert (metrics / "pumpswap_prime_strict_report.json").exists()
    assert (tmp_path / "docs" / "PUMPSWAP_PRIME_STRICT.md").exists()


def test_env_files_have_new_flags_once() -> None:
    required = {
        "PUMPSWAP_PRIME_STRICT_ENABLED",
        "PUMPSWAP_PRIME_MIN_TXNS_5M",
        "PUMPSWAP_PRIME_MIN_LIQUIDITY_USD",
        "PUMPSWAP_PRIME_REQUIRE_REAL_LIQUIDITY",
        "PUMPSWAP_PRIME_REQUIRE_ROUTE",
        "PUMPSWAP_PRIME_MAX_PRICE_IMPACT_PCT",
        "PUMPSWAP_PRIME_SHADOW_IF_NOT_STRICT",
    }
    key_sets: list[set[str]] = []
    for name in (".env", ".env.example"):
        if name == ".env" and not Path(name).exists():
            continue  # Personal credentials are deliberately absent in clean/CI checkouts.
        keys = []
        for line in Path(name).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                keys.append(stripped.split("=", 1)[0].strip())
        assert len(keys) == len(set(keys))
        assert required <= set(keys)
        key_sets.append(set(keys))
    # These new settings have safe defaults; do not rewrite personal secrets
    # merely to mirror optional template keys. All previous key parity,
    # required Prime flags and duplicate checks remain enforced.
    optional_defaults = {"BIRTH_ENRICHMENT_ENABLED", "BIRTH_ENRICHMENT_RPM",
        "BIRTH_ENRICHMENT_TIMEOUT_S", "BIRTH_ENRICHMENT_CACHE_SIZE", "BIRTH_ENRICHMENT_NEGATIVE_TTL_S",
        "MEMORY_CACHE_MAX_ENTRIES", "PROVIDER_FAILURE_MAX_ENTRIES", "PROVIDER_FAILURE_RETENTION_S",
        "BIRDEYE_MAX_INFLIGHT", "BIRDEYE_MAX_JOINERS"}
    assert key_sets and all(keys - optional_defaults == key_sets[0] - optional_defaults for keys in key_sets)


@pytest.mark.parametrize("change", ["optional_omitted", "duplicate", "missing_required"])
def test_new_optional_defaults_preserve_private_env_contract(tmp_path, monkeypatch, change):
    required = ["PUMPSWAP_PRIME_STRICT_ENABLED", "PUMPSWAP_PRIME_MIN_TXNS_5M",
        "PUMPSWAP_PRIME_MIN_LIQUIDITY_USD", "PUMPSWAP_PRIME_REQUIRE_REAL_LIQUIDITY",
        "PUMPSWAP_PRIME_REQUIRE_ROUTE", "PUMPSWAP_PRIME_MAX_PRICE_IMPACT_PCT",
        "PUMPSWAP_PRIME_SHADOW_IF_NOT_STRICT"]
    original = "\n".join(f"{key}=1" for key in required) + "\n"
    personal = original
    if change == "duplicate": personal += f"{required[0]}=1\n"
    elif change == "missing_required": personal = "\n".join(original.splitlines()[1:]) + "\n"
    (tmp_path / ".env").write_text(personal, encoding="utf-8")
    (tmp_path / ".env.example").write_text(original + "BIRTH_ENRICHMENT_ENABLED=true\nBIRTH_ENRICHMENT_RPM=12\n"
        "BIRTH_ENRICHMENT_TIMEOUT_S=3\nBIRTH_ENRICHMENT_CACHE_SIZE=2048\nBIRTH_ENRICHMENT_NEGATIVE_TTL_S=30\n"
        "MEMORY_CACHE_MAX_ENTRIES=8192\nPROVIDER_FAILURE_MAX_ENTRIES=8192\nPROVIDER_FAILURE_RETENTION_S=3600\n"
        "BIRDEYE_MAX_INFLIGHT=16\nBIRDEYE_MAX_JOINERS=256\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    if change == "optional_omitted": test_env_files_have_new_flags_once()
    else:
        with pytest.raises(AssertionError): test_env_files_have_new_flags_once()
    assert (tmp_path / ".env").read_text(encoding="utf-8") == personal
