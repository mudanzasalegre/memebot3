"""Nullable birth age through scheduling, policy helpers and actual entry prefix."""
import ast
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analytics import filters, requeue_policy, sizing
from analytics.token_time import compute_age_minutes
from runtime import candidate_priority
from test_entry_observation import MINT, observed, preparation_namespace, run_function
from test_pump_live_floor import _load_quality_namespace

NOW = dt.datetime(2026, 4, 6, 15, tzinfo=dt.timezone.utc)
UNKNOWN = [{}, {"queue_age_minutes": 0}, {"age_minutes": True},
    {"age_min": -1}, {"age_minutes": float("inf")}, {"created_at": True},
    {"created_at": NOW + dt.timedelta(seconds=1)}]


@pytest.fixture
def basic_context(monkeypatch):
    monkeypatch.setattr(filters, "utc_now", lambda: NOW)
    monkeypatch.setattr(filters, "TRADING_HOURS", "")
    monkeypatch.setattr(filters, "TRADING_HOURS_EXTRA", "")
    monkeypatch.setattr(filters, "BLOCK_HOURS", "")
    monkeypatch.setattr(filters, "MAX_AGE_DAYS", 7)
    monkeypatch.setattr(filters, "MAX_24H_VOLUME", 1e12)
    monkeypatch.setattr(filters, "effective_thresholds", lambda token:
        filters.FilterThresholds("dex", .5, 1, 1000, 1000, 1000, 1000000))
    return {"address": MINT, "chainId": "solana", "liquidity_usd": 10000,
        "volume_24h_usd": 100000, "market_cap_usd": 50000, "holders": 100,
        "txns_last_5m": 100, "price_pct_5m": 10}


@pytest.mark.parametrize("token", UNKNOWN)
def test_basic_age_unknown_is_a_wait_not_a_definitive_rejection(basic_context, token):
    assert filters.basic_filters({**basic_context, **token}) is None


@pytest.mark.parametrize("field", ["age_minutes", "age_min"])
def test_filter_measured_zero_is_exact_and_can_prove_initial_pressure(basic_context, field):
    token = {**basic_context, field: 0, "txns_last_5m_buys": 20,
        "txns_last_5m_sells": 80, "price_pct_5m": 2}
    assert filters._token_age_seconds(token) == 0
    assert filters.has_toxic_initial_sell_pressure(token)


@pytest.mark.parametrize("factor", [1, 1000, 1000000, 1000000000])
def test_filter_supports_valid_numeric_birth_without_erasing_age(basic_context, factor):
    token = {**basic_context, "created_at": int((NOW-dt.timedelta(minutes=8)).timestamp())*factor}
    assert filters._token_age_seconds(token) == 480
    assert filters.basic_filters(token) is True


@pytest.mark.parametrize("age, expected", [(0, None), (.5, True), (8, True), (10081, False)])
def test_existing_filter_age_limits_and_measured_zero_are_preserved(basic_context, age, expected):
    assert filters.basic_filters({**basic_context, "age_minutes": age}) is expected


@pytest.mark.parametrize("identity", [{"chainId": "ethereum"}, {"address": "not-a-mint"}])
def test_invalid_identity_remains_a_hard_filter_failure_even_without_age(basic_context, identity):
    assert filters.basic_filters({**basic_context, **identity}) is False


@pytest.mark.parametrize("queue_age", [0, 1, 19, 20, 99])
def test_queue_residence_is_not_a_newborn_priority_bonus(monkeypatch, queue_age):
    monkeypatch.setattr(candidate_priority, "learned_runner_priority", lambda token: {"bonus": 0})
    unknown = candidate_priority.candidate_priority_score({}, source="pumpportal", now=NOW)
    queued = candidate_priority.candidate_priority_score({"queue_age_minutes": queue_age}, source="pumpportal", now=NOW)
    assert queued == unknown == 35


@pytest.mark.parametrize("birth", [NOW-dt.timedelta(minutes=3), int((NOW-dt.timedelta(minutes=3)).timestamp())*1000])
def test_actual_birth_controls_priority_not_stale_measured_or_queue_age(monkeypatch, birth):
    monkeypatch.setattr(candidate_priority, "learned_runner_priority", lambda token: {"bonus": 0})
    score = candidate_priority.candidate_priority_score({"created_at": birth,
        "age_minutes": 0, "queue_age_minutes": 0}, source="pumpportal", now=NOW)
    assert score == 35 + 17*1.5


@pytest.mark.parametrize("field", ["age_minutes", "age_min"])
def test_zero_age_dex_is_classified_early_and_unknown_is_not(field):
    assert sizing.classify_entry_regime({"discovered_via": "dex", field: 0}) == "pump_early"
    assert sizing.classify_entry_regime({"discovered_via": "dex", field: True}) == "dex_mature"
    assert sizing.classify_entry_regime({"discovered_via": "dex", "queue_age_minutes": 0}) == "dex_mature"


def test_sizing_original_birth_overrides_stale_age(monkeypatch):
    monkeypatch.setattr(sizing, "compute_age_minutes", lambda token: compute_age_minutes(token, now=NOW), raising=False)
    assert sizing.classify_entry_regime({"discovered_via": "dex", "age_minutes": 1,
        "created_at": NOW-dt.timedelta(days=2)}) == "dex_mature"


@pytest.mark.parametrize("source", ["pumpportal", "pump_portal", "pumpfun", "pump_fun"])
def test_pump_discovery_aliases_keep_conservative_early_exposure(source):
    assert sizing.classify_entry_regime({"discovered_via": source}) == "pump_early"


@pytest.mark.parametrize("token", UNKNOWN)
def test_actual_main_helper_preserves_unknown_instead_of_fabricating_zero(token):
    ns = _load_quality_namespace(compute_age_minutes=compute_age_minutes)
    assert ns["_candidate_age_minutes"](token) is None


@pytest.mark.parametrize("token, expected", [({"age_minutes": 0}, 0),
    ({"age_min": 2}, 2), ({"created_at": int((NOW-dt.timedelta(minutes=3)).timestamp())*1000000}, 3),
    ({"created_at": NOW-dt.timedelta(minutes=8), "age_min": 1}, 8)])
def test_actual_main_helper_uses_common_birth_clock(token, expected):
    ns = _load_quality_namespace(compute_age_minutes=compute_age_minutes)
    assert ns["_candidate_age_minutes"](token) == expected


@pytest.mark.parametrize("token", UNKNOWN)
def test_requeue_unknown_age_uses_bounded_observation_wait_not_too_young_budget(monkeypatch, token):
    monkeypatch.setattr(requeue_policy, "utc_now", lambda: NOW)
    monkeypatch.setenv("TRADING_HOURS", "")
    monkeypatch.setenv("TRADING_HOURS_EXTRA", "")
    assert requeue_policy.decide(token, attempts=99, first_seen=NOW.timestamp()) == (True, 5, "entry_observation:missing_age")


@pytest.mark.parametrize("function, args, expected", [
    ("_evaluate_sniper_core", (99,), "age_missing"),
    ("_evaluate_sniper_micro", (99,), "age_missing"),
    ("_meteor_prime_failures", (), "meteor_age_missing"),
    ("_breakout_probe_failures", (99,), "breakout_age_missing")])
def test_actual_leaf_profiles_preserve_missing_age_even_with_other_strong_signals(function, args, expected):
    ns = _load_quality_namespace(compute_age_minutes=compute_age_minutes)
    assert ns[function]({"score_total": 100, "queue_age_minutes": 0}, *args) == [expected]


def test_actual_lane_tag_cannot_turn_missing_birth_into_paper_promotion():
    ns = _load_quality_namespace(compute_age_minutes=compute_age_minutes)
    token = {"entry_lane": "pump_early_paper_bootstrap_micro"}
    before = dict(token)
    assert ns["_tag_pump_sniper_gate"](token) == (False, "missing_age")
    assert token == before


@pytest.mark.parametrize("minimum, maximum", [(1, 20), (0, 20), (1, 0), (0, 0)])
def test_actual_numeric_bounds_are_nullable_and_missing_marker_is_deduplicated(minimum, maximum):
    ns = _load_quality_namespace(compute_age_minutes=compute_age_minutes)
    failures = []
    ns["_add_min_failure"](failures, "age", None, minimum)
    ns["_add_max_failure"](failures, "age", None, maximum)
    assert failures == (["age_missing"] if minimum or maximum else [])


@pytest.mark.parametrize("regime", ["pump_early", "dex_mature", "revival"])
@pytest.mark.parametrize("paper", [False, True])
def test_actual_quality_cannot_grant_unknown_age_a_paper_lane_bypass(regime, paper):
    ns = _load_quality_namespace(compute_age_minutes=compute_age_minutes, DRY_RUN=paper)
    token = {"entry_lane": "pump_early_paper_bootstrap_micro", "queue_age_minutes": 0}
    assert ns["_entry_quality_gate"](token, regime) == (False, "missing_age")


def test_actual_profit_gate_handles_missing_age_before_alternative_promotions():
    ns = _load_quality_namespace(compute_age_minutes=compute_age_minutes,
        CFG=SimpleNamespace(PUMP_EARLY_PRECISION_GATE_ENABLED=True))
    token = {"dex_id": "pumpswap", "has_jupiter_route": 1, "price_usd": 1,
        "liquidity_usd": 10000, "market_cap_usd": 20000, "price_pct_5m": 10}
    result = ns["_evaluate_pumpswap_profit_gate"](token)
    assert result["allowed"] is False and result["reject_reasons"] == ["missing_age"]
    assert result["research_eligible"] is False
    assert "entry_lane" not in token


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["pumpfun", "pumpportal", "dex", "revival"])
async def test_actual_entry_prefix_waits_for_unknown_birth_before_proxy_or_financial_label(source):
    ns, waits, provider = preparation_namespace(observed())
    ns["_candidate_age_minutes"] = lambda token: compute_age_minutes(token, now=NOW)
    ns["warn_if_nulls"] = lambda *a, **k: None
    result = await ns["prepare"]({"address": MINT, "discovered_via": source},
        SimpleNamespace(scalar=AsyncMock(return_value=None)))
    assert result is None and provider.await_count == 1
    assert waits == [{"reason": "missing_age", "stage": "entry_snapshot"}]
    assert ns["_stats"]["filtered_out"] == 0
    ns["_maybe_apply_green_sniper_liquidity_proxy"].assert_not_awaited()
    ns["_maybe_apply_paper_sniper_liquidity_proxy"].assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_birth_restores_actual_entry_prefix_without_cached_age_resurrection():
    ns, waits, provider = preparation_namespace(observed(created_at=NOW-dt.timedelta(minutes=3)))
    ns["_candidate_age_minutes"] = lambda token: compute_age_minutes(token, now=NOW)
    ns["warn_if_nulls"] = lambda *a, **k: None
    candidate, receipt = await ns["prepare"]({"address": MINT, "age_minutes": 999,
        "queue_age_minutes": 999}, SimpleNamespace(scalar=AsyncMock(return_value=None)))
    assert receipt is not None and not waits and provider.await_count == 1
    assert compute_age_minutes(candidate, now=NOW) == 3


def test_actual_common_age_wait_precedes_all_admission_fast_paths():
    node = run_function("_evaluate_and_buy")
    body = ast.unparse(node)
    assert body.index("reason='missing_age', stage='entry_snapshot'") < body.index("_maybe_apply_green_sniper_liquidity_proxy(")
    assert body.index("reason='missing_age', stage='entry_snapshot'") < body.index("_maybe_apply_paper_bootstrap(")
    assert body.index("reason='missing_age', stage='entry_snapshot'") < body.index("_store_policy_reject(token, reason='no_liq')")


@pytest.mark.asyncio
async def test_unknown_age_cannot_probe_a_paper_liquidity_route():
    function = run_function("_maybe_apply_paper_sniper_liquidity_proxy")
    probe = AsyncMock(side_effect=AssertionError("Age wait must precede provider route probe"))
    ns = {"DRY_RUN": True, "_PUMP_EARLY_SNIPER_ENABLED": True,
        "_PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_ENABLED": True,
        "_PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_MIN_AGE_MIN": 0,
        "_candidate_age_minutes": lambda token: None, "_probe_jupiter_route": probe}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "run_bot.py", "exec"), ns)
    assert await ns[function.name]({"entry_regime": "pump_early"}, MINT) is False
    probe.assert_not_awaited()


def test_unknown_age_cannot_promote_a_green_shadow_runner_canary():
    function = run_function("_green_shadow_can_continue_to_runner_canary")
    ns = {"CFG": SimpleNamespace(), "_metric_optional_float": lambda *a: 1,
        "_metric_float": lambda token, field: {"liquidity_usd": 2000,
            "market_cap_usd": 20000, "price_impact_pct": 1}[field],
        "_candidate_age_minutes": lambda token: None, "_gate_dex_id": lambda token: "pumpfun",
        "_is_liquidity_proxy": lambda token: False}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "run_bot.py", "exec"), ns)
    token = {"discovered_via": "pumpfun"}
    decision = SimpleNamespace(action="shadow", paper_birth_probe=False, reject_reasons=["low_green_momentum"])
    assert ns[function.name](token, decision) is False
    assert "green_runner_canary_candidate" not in token
