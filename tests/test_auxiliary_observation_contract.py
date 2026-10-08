"""Synthetic original receipts only: no provider/order or runtime-data access."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import datetime as dt
import json
from pathlib import Path
import time

import pytest

from analytics import filters, insider, trend
from analytics.social_signal import apply_social_signal_to_token, unknown_social_signal
from features.builder import build_feature_vector, COLUMNS
from fetcher import rugcheck, helius_cluster
from runtime import auxiliary_enrichment as enrichment, entry_observation as entry, trade_learning as learning
from utils import auxiliary_observation as aux, simple_cache
from utils.data_utils import apply_default_values
from utils.market_observation import stamp_market_observation

MINT = "A" * 32


@pytest.fixture(autouse=True)
def isolated_cache():
    simple_cache._CACHE.clear()
    yield
    simple_cache._CACHE.clear()


def token(**updates):
    now = time.time()
    raw = {"address": MINT, "price_usd": 1., "price_pct_5m": 20.,
        "txns_last_5m_buys": 10, "txns_last_5m_sells": 2, "txns_last_5m": 12,
        "liquidity_usd": 10000., "liquidity_is_proxy": 0, "liquidity_usd_is_proxy": 0,
        "created_at": dt.datetime.fromtimestamp(now - 120, dt.timezone.utc).isoformat(), **updates}
    return stamp_market_observation(raw, "dexscreener", received_at=now - 1)


def concentration(**updates):
    now = time.time() - 1
    return {"accounts": [{"address": f"account-{n}", "amount": "100", "decimals": 6} for n in range(20)],
        "total_supply": "10000", "decimals": 6, "largest_slot": 100, "supply_slot": 101,
        "largest_received_at": now, "supply_received_at": now, "commitment": "confirmed", **updates}


def rehash(record):
    record["payload_sha256"] = aux._hash({k: v for k, v in record.items() if k != "payload_sha256"})
    return record


@pytest.mark.parametrize("pct, expected", [(.5, 0), (14.999, 0), (15, 1), (-15, -1), (-.5, 0), (100000., 1)])
def test_momentum_percentage_points_without_upside_cap(pct, expected):
    raw = aux.trend_observation(token(price_pct_5m=pct))
    assert raw["value"] == expected
    assert aux.checked_auxiliary_observation(raw, MINT, "trend") == raw


@pytest.mark.parametrize("field, value", [("price_pct_5m", None), ("price_pct_5m", True),
    ("price_pct_5m", float("inf")), ("txns_last_5m_buys", None), ("txns_last_5m_buys", .5),
    ("txns_last_5m_buys", 2**31), ("created_at", "2026-10-01T00:00:00"),
    ("liquidity_usd", float("nan")), ("liquidity_is_proxy", True)])
def test_missing_or_invalid_accumulation_is_not_clean_insider_evidence(field, value):
    row = token(**{field: value})
    assert aux.accumulation_observation(row)["value"] is None


@pytest.mark.parametrize("updates, expected", [({"price_pct_5m": .5}, False),
    ({"txns_last_5m_buys": 1, "txns_last_5m": 100}, False),
    ({"price_pct_5m": 5., "txns_last_5m_buys": 3}, True),
    ({"price_pct_5m": 100000.}, True), ({"liquidity_usd": 2999.}, False)])
def test_actual_buys_not_total_transactions_drive_positive_proxy(updates, expected):
    raw = aux.accumulation_observation(token(**updates))
    assert raw["value"] is expected
    assert aux.checked_auxiliary_observation(raw, MINT, "early_accumulation") == raw


def test_future_creation_stale_or_unproved_snapshot_stays_unknown():
    future = dt.datetime.fromtimestamp(time.time() + 60, dt.timezone.utc).isoformat()
    assert aux.accumulation_observation(token(created_at=future))["value"] is None
    raw = token()
    raw["market_observation"]["fields"]["price_pct_5m"]["received_at"] -= 60
    assert aux.trend_observation(raw)["value"] is None
    raw.pop("market_observation")
    assert aux.accumulation_observation(raw)["value"] is None


@pytest.mark.asyncio
async def test_compatibility_wrappers_use_snapshot_without_chart_or_second_get(monkeypatch):
    async def forbidden(*a, **kw):
        raise AssertionError("unexpected provider read")
    monkeypatch.setattr(trend, "_fetch_closes", forbidden)
    monkeypatch.setattr("fetcher.dexscreener.get_pair", forbidden)
    row = token()
    assert await trend.trend_signal(MINT, snapshot=row) == ("up", True)
    assert await insider.insider_alert(MINT, snapshot=row) is True
    assert await trend.trend_signal("other", snapshot=row) == ("unknown", True)
    assert await insider.insider_alert("other", snapshot=row) is None


@pytest.mark.parametrize("score", [None, True, 1.5, -1, 101, float("inf"), "invalid"])
def test_unknown_or_invalid_rug_does_not_receive_safe_points(score):
    assert filters.total_score({"rug_score": score}) == 0


def test_soft_score_risk_direction_typed_unknowns_and_positive_momentum():
    assert filters.total_score({}) == 0
    assert filters.total_score({"rug_score": 30}) == 15
    assert filters.total_score({"rug_score": 31}) == 0
    assert filters.total_score({"rug_score": 99}) == 0
    assert filters.total_score({"cluster_bad": "false"}) == 15
    assert filters.total_score({"cluster_bad": "true"}) == 0
    assert filters.total_score({"social_ok": "false", "insider_sig": False}) == 0
    assert filters.total_score({"early_accumulation_sig": True}) == 10
    defaults = apply_default_values({})
    assert all(defaults[k] is None for k in ("cluster_bad", "mint_auth_renounced", "insider_sig"))


@pytest.mark.parametrize("pct, expected", [(.5, True), (-.5, True), (5., False),
    (None, False), (float("inf"), False), (True, False)])
def test_toxic_pressure_uses_canonical_percent_and_never_guesses_fraction(pct, expected):
    row = {"age_min": 2, "txns_last_5m": 100, "txns_last_5m_sells": 80,
        "price_pct_5m": pct, "priceChange": {"m5": 999}}
    assert filters.has_toxic_initial_sell_pressure(row) is expected
    row["txns_last_5m_sells"] = 80.5
    assert not filters.has_toxic_initial_sell_pressure(row)


@pytest.mark.parametrize("mutation", ["duplicate", "unsorted", "zero_supply", "float_supply", "wrong_decimals",
    "far_slot", "wrong_commitment", "truncated", "over_supply", "bool_amount", "string_slot"])
def test_rpc_concentration_rejects_incoherent_source_inputs(mutation):
    raw = concentration()
    if mutation == "duplicate": raw["accounts"][1]["address"] = raw["accounts"][0]["address"]
    elif mutation == "unsorted": raw["accounts"][-1]["amount"] = "101"
    elif mutation == "zero_supply": raw["total_supply"] = "0"
    elif mutation == "float_supply": raw["total_supply"] = 10000.
    elif mutation == "wrong_decimals": raw["accounts"][0]["decimals"] = 9
    elif mutation == "far_slot": raw["supply_slot"] = 109
    elif mutation == "wrong_commitment": raw["commitment"] = "processed"
    elif mutation == "truncated": raw["accounts"] = raw["accounts"][:5]
    elif mutation == "over_supply": raw["total_supply"] = "1000"
    elif mutation == "bool_amount": raw["accounts"][0]["amount"] = True
    elif mutation == "string_slot": raw["largest_slot"] = "100"
    assert aux.cluster_inputs_value(raw) is None


def test_rpc_concentration_uses_exact_uint64_boundary_and_complete_small_population():
    assert aux.cluster_inputs_value(concentration()) is False
    raw = concentration(total_supply=str(2**64 - 1))
    boundary = (2**64 - 1) // 5
    raw["accounts"][0]["amount"] = str(boundary)
    for account in raw["accounts"][1:]: account["amount"] = "0"
    assert aux.cluster_inputs_value(raw) is False
    raw["accounts"][0]["amount"] = str(boundary + 1)
    assert aux.cluster_inputs_value(raw) is True
    raw = concentration(accounts=[{"address": "sole-account", "amount": "10000", "decimals": 6}])
    assert aux.cluster_inputs_value(raw) is True


class Response:
    status = 200
    def __init__(self, data): self.data = data
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def json(self): return deepcopy(self.data)


class Session:
    def __init__(self, responder): self.responder = responder
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    def get(self, url, **kw): return Response(self.responder(url, kw))
    def post(self, url, **kw): return Response(self.responder(url, kw))


@pytest.mark.asyncio
async def test_rug_documented_public_route_original_receipt_and_non_sliding_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(rugcheck, "RUGCHECK_API_BASE", "https://api.rugcheck.xyz/v1")
    monkeypatch.setattr(rugcheck, "HEADERS", {})
    def response(url, kw):
        calls.append((url, kw))
        return {"score": 1234, "score_normalised": 12}
    monkeypatch.setattr(rugcheck.aiohttp, "ClientSession", lambda **kw: Session(response))
    first = await rugcheck.fetch_observation(MINT)
    assert first["value"] == 12 and first["inputs"]["score"] == 1234
    assert calls == [(f"https://api.rugcheck.xyz/v1/tokens/{MINT}/report/summary", {"headers": {}})]
    first["inputs"]["score"] = 999
    again = await rugcheck.fetch_observation(MINT)
    assert again["inputs"]["score"] == 1234 and len(calls) == 1
    cache_key = f"rug:receipt:v1:{MINT}"
    expires, cached = simple_cache._CACHE[cache_key]
    original_clock = cached["observed_at"]
    assert again["observed_at"] == original_clock
    cached["observed_at"] -= 121
    rehash(cached)
    assert (await rugcheck.fetch_observation(MINT))["observed_at"] >= original_clock
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{}, {"score": 12}, {"score": 5, "score_normalised": True},
    {"score": 5, "score_normalised": 1.5}, {"score": 5, "score_normalised": 101},
    {"mint": "wrong", "score": 5, "score_normalised": 5}, {"score": -1, "score_normalised": 0}])
async def test_rug_provider_invalid_or_missing_normalisation_is_unknown(monkeypatch, bad):
    monkeypatch.setattr(rugcheck, "RUGCHECK_API_BASE", "https://public.example")
    monkeypatch.setattr(rugcheck.aiohttp, "ClientSession", lambda **kw: Session(lambda *a: bad))
    assert await rugcheck.check_token(MINT) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["wrong_id", "error", "wrong_protocol", "no_result", "ok"])
async def test_rpc_envelope_validation_and_client_clock(monkeypatch, bad):
    def response(url, kw):
        request = kw["json"]
        data = {"jsonrpc": "2.0", "id": request["id"], "result": {"context": {"slot": 1}, "value": []},
                "received_at": 999999999999.}
        if bad == "wrong_id": data["id"] = "wrong"
        elif bad == "error": data["error"] = {"code": -1}
        elif bad == "wrong_protocol": data["jsonrpc"] = "1.0"
        elif bad == "no_result": data.pop("result")
        return data
    monkeypatch.setattr(helius_cluster, "HELIUS_RPC_URL", "https://rpc.example")
    monkeypatch.setattr(helius_cluster.aiohttp, "ClientSession", lambda **kw: Session(response))
    actual = await helius_cluster._rpc("getTokenSupply", [MINT, {"commitment": "confirmed"}])
    if bad == "ok": assert actual["received_at"] < 999999999999.
    else: assert actual is None


@pytest.mark.asyncio
async def test_rpc_heads_parallel_original_clocks_and_cache_age(monkeypatch):
    inputs, arrived, release = concentration(), [], asyncio.Event()
    async def rpc(method, params):
        assert params == [MINT, {"commitment": "confirmed"}]
        arrived.append(method)
        if len(arrived) == 2: release.set()
        await asyncio.wait_for(release.wait(), timeout=.5)
        result = {"context": {"slot": 100}, "value": inputs["accounts"] if method == "getTokenLargestAccounts"
                  else {"amount": inputs["total_supply"], "decimals": 6}}
        return {"result": result, "received_at": inputs["largest_received_at"]}
    monkeypatch.setattr(helius_cluster, "_rpc", rpc)
    first = await helius_cluster.fetch_observation(MINT)
    assert first["value"] is False and first["observed_at"] == inputs["largest_received_at"]
    assert await helius_cluster.suspicious_cluster(MINT) is False and len(arrived) == 2
    first["value"] = True
    assert (await helius_cluster.fetch_observation(MINT))["value"] is False
    expires, cached = simple_cache._CACHE[f"rpc:concentration:receipt:v1:{MINT}"]
    cached["observed_at"] -= 61
    rehash(cached)
    await helius_cluster.fetch_observation(MINT)
    assert len(arrived) == 4


@pytest.mark.asyncio
async def test_bounded_risk_failure_keeps_independent_valid_peer_and_cancellation_drains(monkeypatch):
    row, cancelled = token(), asyncio.Event()
    enrichment.prepare_cheap_auxiliary(row)
    async def slow(address):
        try: await asyncio.Future()
        finally: cancelled.set()
    async def known(address):
        return aux.observation("rug", address, 10, source="rugcheck_report_summary", observed_at=time.time(),
            inputs={"score": 500, "score_normalised": 10})
    monkeypatch.setattr(helius_cluster, "fetch_observation", slow)
    monkeypatch.setattr(rugcheck, "fetch_observation", known)
    monkeypatch.setattr(enrichment, "RISK_DEADLINE_S", .01)
    await enrichment.enrich_entry_risk(row)
    assert row["rug_score"] == 10 and row["cluster_bad"] is None and cancelled.is_set()
    assert row["insider_sig"] is None and row["early_accumulation_sig"] is True
    await enrichment.enrich_entry_risk(row, skip=True)
    assert row["rug_score"] is None and row["cluster_bad"] is None


@pytest.mark.asyncio
async def test_parent_cancellation_drains_both_owned_risk_heads(monkeypatch):
    arrived, drained, both = [], [], asyncio.Event()
    async def pending(address):
        arrived.append(address)
        if len(arrived) == 2: both.set()
        try: await asyncio.Future()
        finally: drained.append(address)
    monkeypatch.setattr(helius_cluster, "fetch_observation", pending)
    monkeypatch.setattr(rugcheck, "fetch_observation", pending)
    row = token()
    enrichment.prepare_cheap_auxiliary(row)
    worker = asyncio.create_task(enrichment.enrich_entry_risk(row))
    await asyncio.wait_for(both.wait(), timeout=.5)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError): await worker
    assert len(drained) == 2


def test_future_provider_receipt_is_not_prepared_as_positive_current_momentum():
    row = stamp_market_observation(token(), "dexscreener", received_at=time.time() + 1)
    enrichment.prepare_cheap_auxiliary(row)
    assert row["trend"] is None and row["early_accumulation_sig"] is None


def test_one_future_market_component_does_not_hide_behind_earlier_receipt():
    row = token()
    row["market_observation"]["fields"]["txns_last_5m_buys"]["received_at"] = time.time() + 1
    enrichment.prepare_cheap_auxiliary(row)
    assert row["trend"] == 1 and row["early_accumulation_sig"] is None


def frozen():
    row = token()
    enrichment.prepare_cheap_auxiliary(row)
    apply_social_signal_to_token(row, unknown_social_signal())
    original = entry.freeze_entry_observation(row, paper=True)
    original = entry.freeze_entry_social_observation(row, original)
    original = entry.freeze_entry_auxiliary_observation(row, original)
    vector = build_feature_vector(row)
    return row, original, vector


@pytest.mark.parametrize("mutation", ["receipt", "scalar", "vector", "expired", "mint"])
def test_immutable_auxiliary_input_is_rechecked_before_intent(monkeypatch, mutation):
    row, original, vector = frozen()
    assert entry.entry_observation_problem(row, original, paper=True, vector=vector.to_dict()) is None
    if mutation == "receipt": row["auxiliary_observations"]["trend"]["observed_at"] += .1
    elif mutation == "scalar": row["early_accumulation_sig"] = False
    elif mutation == "vector": vector["trend"] = -1
    elif mutation == "mint": row["address"] = "other"
    elif mutation == "expired": monkeypatch.setitem(aux.MAX_AGES, "trend", .01)
    assert entry.entry_observation_problem(row, original, paper=True, vector=vector.to_dict()) is not None


def test_v3_full_proof_preserves_v1_v2_schemas_and_original_history(monkeypatch):
    row, original, vector = frozen()
    captured = dt.datetime.now(dt.timezone.utc)
    receipts = entry.entry_auxiliary_observations(original)
    proof = learning.freeze_entry_features(vector, address=MINT, captured_at=captured, auxiliary_observations=receipts)
    assert proof["version"] == learning.ENTRY_ALL_AUX_VERSION and set(proof["vector"]) == set(COLUMNS)
    receipts["trend"]["value"] = -1
    assert proof["auxiliary_observations"]["trend"]["value"] == 1
    monkeypatch.setattr(aux.time, "time", lambda: captured.timestamp() + 999999.)
    learning.validate_entry_features(proof, address=MINT)
    legacy = learning.freeze_entry_features(vector, address=MINT, captured_at=captured)
    assert legacy["version"] == learning.ENTRY_VERSION
    social_only = learning.freeze_entry_features(vector, address=MINT, captured_at=captured,
        auxiliary_observations={"social": row["social_signal"]})
    assert social_only["version"] == learning.ENTRY_AUX_VERSION


@pytest.mark.parametrize("mutation", ["wrong_hash", "wrong_value", "wrong_source", "future_clock", "wrong_vector", "extra_kind",
    "changed_pct_same_direction", "changed_buys_same_proxy", "proxy_liquidity"])
def test_v3_rejects_forged_or_noncausal_proofs_even_after_outer_rehash(mutation):
    row, original, vector = frozen()
    captured = dt.datetime.now(dt.timezone.utc)
    proof = learning.freeze_entry_features(vector, address=MINT, captured_at=captured,
        auxiliary_observations=entry.entry_auxiliary_observations(original))
    record = proof["auxiliary_observations"]["trend"]
    if mutation == "wrong_hash": record["payload_sha256"] = "0" * 64
    elif mutation == "wrong_value": record["value"] = -1
    elif mutation == "wrong_source": record["source"] = "invented"
    elif mutation == "future_clock": record["evaluated_at"] = captured.timestamp() + 1
    elif mutation == "wrong_vector": proof["vector"]["trend"] = -1
    elif mutation == "extra_kind": proof["auxiliary_observations"]["invented"] = {}
    elif mutation == "changed_pct_same_direction": proof["vector"]["price_pct_5m"] = 25.
    elif mutation == "changed_buys_same_proxy": proof["vector"]["txns_last_5m_buys"] = 20
    elif mutation == "proxy_liquidity": proof["vector"]["liquidity_is_proxy"] = 1
    if mutation != "wrong_hash": rehash(record)
    rehash(proof)
    with pytest.raises(learning.TradeLearningError): learning.validate_entry_features(proof, address=MINT)


def test_actual_common_entry_path_prepares_before_ranking_and_freezes_after_bounded_risk():
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    nodes = [item for item in ast.walk(tree) if isinstance(item, ast.Call)]
    def line(name): return next(n.lineno for n in nodes if getattr(n.func, "id", None) == name)
    assert line("prepare_cheap_auxiliary") < line("evaluate_green_sniper")
    assert line("enrich_entry_risk") < line("freeze_entry_auxiliary_observation")
    assert not any(getattr(n.func, "attr", "") in {"trend_signal", "insider_alert", "suspicious_cluster", "check_token"} for n in nodes)


def test_causal_clock_matches_microsecond_serialization_without_seconds_tolerance():
    stamp = 1791425261.6302795
    assert aux.receipt_datetime(stamp).isoformat() == "2026-10-08T02:07:41.630279+00:00"
    assert aux.receipt_datetime(stamp + .000002) > aux.receipt_datetime(stamp)
    assert not aux.clock_after(stamp + .000001, stamp)
    assert aux.clock_after(stamp + .000002, stamp)
