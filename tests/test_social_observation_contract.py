"""Original social HTTP receipts, nonblocking entry use and causal exports.

All provider responses and orders are synthetic; no runtime artifacts are used.
"""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from dataclasses import replace
import datetime as dt
import json
from pathlib import Path
import time
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from analytics import social_signal as social
from fetcher import dexscreener, socials
from features.builder import build_feature_vector, COLUMNS
from features.numeric_encoding import PREFIX
from ml.feature_matrix import coerce_feature_frame
from runtime import entry_observation as entry, social_enrichment_queue as queue, trade_learning as learning
from utils import price_service, simple_cache
from utils.market_observation import stamp_market_observation

MINT = "A" * 32
OTHER = "B" * 32
NOW = 1_791_425_000.


def pair(address=MINT, **extra):
    return {"chainId": "solana", "baseToken": {"address": address, "symbol": "TEST"},
        "priceUsd": "1", "liquidity": {"usd": 10000},
        "info": {"websites": [{"url": "https://token.example"}],
                 "socials": [{"type": "twitter", "url": "https://x.com/test_token"}]}, **extra}


def observed_social(*, received=NOW, address=MINT):
    return social.social_signal_from_profile(pair(address), address=address, received_at=received)


@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    simple_cache._CACHE.clear()
    monkeypatch.setattr(social, "CFG", SimpleNamespace(SOCIALS_CACHE_TTL_S=600,
        GREEN_SNIPER_SOCIALS_SCORE_BONUS=5))
    yield
    simple_cache._CACHE.clear()


@pytest.mark.parametrize("info, expected", [
    ({"websites": [], "socials": []}, "missing"),
    ({"websites": [{"url": "token.example"}], "socials": []}, "present"),
    ({"websites": [], "socials": [{"platform": "twitter", "handle": "test_token"}]}, "present"),
    ({"websites": [], "socials": [{"platform": "telegram", "handle": "test_token"}]}, "present"),
    (None, "unknown"), ({}, "unknown"), ({"socials": "broken"}, "unknown"),
    ({"socials": [None]}, "unknown"), ({"websites": [{"url": {"nested": "not-url"}}]}, "unknown"),
    ({"socials": [{"platform": "discord", "handle": "not_an_invite"}]}, "unknown"),
    ({"socials": [{"platform": "twitter", "handle": "bad/name"}]}, "unknown"),
])
def test_documented_pair_info_and_bounded_handle_variants(info, expected):
    signal = social.social_signal_from_profile(pair(info=info), address=MINT, received_at=NOW)
    assert signal.status == expected
    assert social.checked_social_receipt(signal, MINT, now=NOW + 1) is not None
    if expected == "unknown":
        assert signal.social_ok is None


@pytest.mark.parametrize("info, unreported", [
    ({"websites": [{"url": "https://token.example"}]}, "twitter_present"),
    ({"socials": [{"type": "twitter", "url": "https://x.com/token"}]}, "website_present"),
])
def test_partial_metadata_keeps_positive_links_without_inventing_absent_channels(info, unreported):
    signal = social.social_signal_from_profile(pair(info=info), address=MINT, received_at=NOW)
    assert signal.social_ok is True and signal.link_count == 1
    assert social.checked_social_receipt(signal, MINT, now=NOW + 1) == signal
    vector = build_feature_vector({"address": MINT, "social_signal": signal.to_dict()})
    assert pd.isna(vector[unreported]) and vector["social_ok"] == 1


@pytest.mark.parametrize("info", [{"websites": []}, {"socials": []}])
def test_partial_empty_metadata_does_not_claim_no_socials(info):
    signal = social.social_signal_from_profile(pair(info=info), address=MINT, received_at=NOW)
    assert signal.status == "unknown" and signal.social_ok is None


@pytest.mark.parametrize("value", [{"url": "https://x.com/fake"}, ["https://x.com/fake"], True,
    4, "javascript:alert(1)", "file:///tmp/test", "https://user:pass@x.com/token", "https://x.com/a b",
    "https://x.com:bad/token", "@user"])
def test_malformed_urls_do_not_fabricate_known_absence_or_presence(value):
    signal = social.social_signal_from_profile({"links": {"twitterUrl": value}})
    assert signal.status == "unknown" and signal.social_ok is None
    vector = build_feature_vector({"address": MINT, "social_signal": signal.to_dict()})
    assert pd.isna(vector["social_ok"]) and pd.isna(vector["twitter_present"])


@pytest.mark.parametrize("value, expected", [(False, False), ("false", False), ("0", False),
    (True, True), ("true", True), ("1", True), (2, None), ("unknown", None), ({}, None)])
def test_legacy_scalar_boolean_is_typed_not_truthiness(value, expected):
    signal = social.social_signal_from_token({"social_ok": value})
    assert signal.social_ok is expected


@pytest.mark.parametrize("field, value", [("social_link_count", 1.5), ("social_link_count", -1),
    ("social_link_count", True), ("social_confidence_bonus", float("inf")),
    ("twitter_present", "perhaps"), ("social_risk_flags", {"broken": 1})])
def test_malformed_legacy_values_are_neutral_and_do_not_raise(field, value):
    signal = social.social_signal_from_token({"social_ok": True, field: value})
    assert signal.status == "unknown" and signal.social_ok is None


@pytest.mark.parametrize("change", [
    {"address": OTHER}, {"received_at": NOW - 601}, {"received_at": NOW + 3},
    {"received_at": True}, {"received_at": "1791425000"}, {"received_at": float("inf")},
    {"receipt_version": "provider-invented"}, {"basis": "provider-market-asof"}, {"basis": []},
    {"social_ok": "true"}, {"twitter_present": 2}, {"link_count": True},
    {"link_count": 9}, {"twitter_url": None}, {"confidence_bonus": float("nan")},
    {"private_key": "synthetic-extra"}, {"source": {}},
])
def test_wrong_identity_expired_future_malformed_or_modified_receipt_is_rejected(change):
    payload = {**observed_social().to_dict(), **change}
    assert social.checked_social_receipt(payload, MINT, now=NOW) is None


def test_known_absence_and_unknown_have_different_all_head_matrix_inputs():
    signals = [social.social_signal_from_profile({"info": {"websites": [], "socials": []}},
        address=MINT, received_at=NOW), social.unknown_social_signal()]
    names = ["social_ok", PREFIX + "social_ok", "twitter_present", PREFIX + "twitter_present",
        "social_link_count", PREFIX + "social_link_count", "social_confidence_bonus", PREFIX + "social_confidence_bonus"]
    raw = pd.DataFrame([build_feature_vector({"address": MINT, "social_signal": item.to_dict()}) for item in signals])
    matrix = coerce_feature_frame(raw, names)
    assert matrix.iloc[0].tolist() == [0., 0., 0., 0., 0., 0., 0., 0.]
    assert matrix.iloc[1].tolist() == [0., 1., 0., 1., 0., 1., 0., 1.]


class Response:
    def __init__(self, body, status=200, error=None):
        self.body, self.status, self.error = body, status, error
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def json(self):
        if self.error:
            raise self.error
        return deepcopy(self.body)


class Session:
    def __init__(self, response, calls):
        self.response, self.calls = response, calls
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def fake_http(monkeypatch, response):
    calls = []
    monkeypatch.setattr(socials.aiohttp, "ClientSession", lambda: Session(response, calls))
    monkeypatch.setattr(socials, "BASE", "https://synthetic.invalid")
    monkeypatch.setattr(socials, "CFG", SimpleNamespace(SOCIALS_TIMEOUT_S=2))
    return calls


@pytest.mark.asyncio
async def test_actual_fetch_uses_documented_route_exact_mint_and_original_detached_cache(monkeypatch):
    calls = fake_http(monkeypatch, Response([pair(OTHER, liquidity={"usd": 1e9}),
        pair(MINT, chainId="ethereum", liquidity={"usd": 1e8}), pair()]))
    first = await socials.fetch_social_profile(MINT)
    payload = first.to_dict()
    payload["twitter_url"] = "https://x.com/modified"
    second = await socials.fetch_social_profile(MINT)
    assert first == second and first.received_at == second.received_at
    assert second.address == MINT and second.twitter_url == "https://x.com/test_token"
    assert calls == [(f"https://synthetic.invalid/token-pairs/v1/solana/{MINT}", {"timeout": 2.})]
    assert social.checked_social_receipt(second, MINT) is not None
    assert await socials.has_socials(MINT) is True
    assert len(calls) == 1 and "social:" + MINT not in simple_cache._CACHE


@pytest.mark.asyncio
@pytest.mark.parametrize("body, status, error", [([], 200, None), ([pair(OTHER)], 200, None),
    ([pair(MINT, chainId="ethereum")], 200, None), ({"pairs": [pair()]}, 200, None),
    ([pair(info=None)], 200, None), (None, 403, None), (None, 429, None),
    (None, 200, ValueError("synthetic bad JSON")), (None, 200, TimeoutError("synthetic timeout"))])
async def test_provider_failures_unknown_not_healthy_and_short_cached(monkeypatch, body, status, error):
    calls = fake_http(monkeypatch, Response(body, status, error))
    first = await socials.fetch_social_profile(MINT)
    assert first.social_ok is None and first.status == "unknown"
    assert social.checked_social_receipt(first, MINT) is not None
    assert (simple_cache._CACHE[f"social_profile:v2:{MINT}"][0] - first.received_at) <= 61
    assert await socials.has_socials(MINT) is None and len(calls) == 1


@pytest.mark.asyncio
async def test_expired_inner_cache_cannot_slide_through_scalar_wrapper(monkeypatch):
    calls = fake_http(monkeypatch, Response([pair(info={"websites": [], "socials": []})]))
    old = observed_social(received=time.time() - 601).to_dict()
    simple_cache.cache_set(f"social_profile:v2:{MINT}", old, ttl=600)
    simple_cache.cache_set(f"social:{MINT}", True, ttl=600)  # obsolete cache must not override
    assert await socials.has_socials(MINT) is False
    assert len(calls) == 1


def test_snapshot_metadata_survives_real_normalization_merge_and_common_entry(monkeypatch):
    raw = pair(social_signal={"address": OTHER, "received_at": time.time() + 100})
    tick = dexscreener._stamp_pair_observation(raw)
    signal = social.checked_social_receipt(tick["social_signal"], MINT)
    assert signal is not None
    assert signal.received_at == tick["market_observation"]["fields"]["price_usd"]["received_at"]
    primary = stamp_market_observation({"address": MINT, "price_usd": 1.1}, "jupiter")
    merged = price_service._merge_market_fields(primary, tick, "dexscreener")
    candidate = entry.prepare_entry_candidate({"address": MINT, "social_ok": True,
        "social_signal": observed_social(address=OTHER).to_dict()}, merged)
    assert candidate["social_signal"] == tick["social_signal"]
    monkeypatch.setattr(queue, "GLOBAL_SOCIAL_ENRICHMENT_QUEUE", queue.SocialEnrichmentQueue())
    assert queue.consume_social_enrichment(candidate) == signal
    assert candidate["price_usd"] == 1.1 and candidate["twitter_present"] == 1
    candidate["social_signal"]["twitter_url"] = "https://x.com/mutated"
    assert tick["social_signal"]["twitter_url"] == "https://x.com/test_token"


@pytest.mark.asyncio
async def test_queue_retains_inner_original_age_and_expires_without_relabeling(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(social.time, "time", lambda: clock[0])
    original = observed_social(received=NOW - 590)
    async def cached_fetch(address):
        return original
    monkeypatch.setattr(queue, "fetch_social_profile", cached_fetch)
    monkeypatch.setattr(queue, "flag_suspicious_links", lambda signal, **kw: signal)
    for name in ("record_social_links", "record_social_signal", "record_runtime_event"):
        monkeypatch.setattr(queue, name, lambda *a, **kw: None)
    q = queue.SocialEnrichmentQueue()
    await q._run(queue.SocialEnrichmentRequest(MINT))
    assert q.get_cached(MINT).received_at == NOW - 590
    clock[0] += 11
    assert q.get_cached(MINT) is None and not q._cache


def test_nonblocking_consumer_uses_background_result_before_green_ranking(monkeypatch):
    signal = observed_social(received=time.time() - 5)
    q = queue.SocialEnrichmentQueue()
    q._cache[MINT] = (signal.received_at, signal)
    monkeypatch.setattr(queue, "GLOBAL_SOCIAL_ENRICHMENT_QUEUE", q)
    token = {"address": MINT, "social_ok": False, "social_status": "missing"}
    assert queue.consume_social_enrichment(token) == signal
    assert token["social_ok"] is True and token["social_received_at"] == signal.received_at
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_evaluate_and_buy")
    calls = list(ast.walk(node))
    consume = next(item for item in calls if isinstance(item, ast.Call) and getattr(item.func, "id", "") == "consume_social_enrichment")
    rank = next(item for item in calls if isinstance(item, ast.Call) and getattr(item.func, "id", "") == "evaluate_green_sniper")
    freeze = next(item for item in calls if isinstance(item, ast.Call) and getattr(item.func, "id", "") == "freeze_entry_social_observation")
    buy = next(item for item in calls if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
               and getattr(item.func.value, "id", "") == "_BUY_RECOVERY" and item.func.attr == "begin")
    assert consume.lineno < rank.lineno < freeze.lineno < buy.lineno
    assert "auxiliary_observations" in {kw.arg for kw in buy.keywords}


def test_newer_unchanged_snapshot_does_not_erase_cached_risk_or_renew_its_age(monkeypatch):
    checked = replace(observed_social(received=time.time() - 10), status="suspicious",
        risk_flags=("reused_link",), confidence_bonus=0., risk_checked_at=time.time() - 5)
    fresh = observed_social(received=time.time() - 1)
    q = queue.SocialEnrichmentQueue()
    q._cache[MINT] = (checked.received_at, checked)
    monkeypatch.setattr(queue, "GLOBAL_SOCIAL_ENRICHMENT_QUEUE", q)
    token = {"address": MINT, "social_signal": fresh.to_dict()}
    assert queue.consume_social_enrichment(token) == checked
    assert token["social_status"] == "suspicious" and token["social_received_at"] == checked.received_at


@pytest.mark.asyncio
async def test_changed_observed_profile_is_enriched_off_thread_without_duplicate_provider_request(monkeypatch):
    thread_ids, events = [], []
    q = queue.SocialEnrichmentQueue()
    prior = observed_social(received=time.time() - 10)
    fresh = replace(observed_social(received=time.time() - 1), twitter_url="https://x.com/new_token")
    q._cache[MINT] = (prior.received_at, prior)
    main_thread = threading.get_ident()
    def flag(signal, **kw):
        thread_ids.append(threading.get_ident())
        return replace(signal, risk_checked_at=time.time())
    async def no_http(address):
        raise AssertionError("Current pair metadata must not trigger a duplicate network fetch")
    monkeypatch.setattr(queue, "fetch_social_profile", no_http)
    monkeypatch.setattr(queue, "flag_suspicious_links", flag)
    monkeypatch.setattr(queue, "record_social_links", lambda *a, **kw: None)
    monkeypatch.setattr(queue, "record_social_signal", lambda *a, **kw: events.append(kw))
    monkeypatch.setattr(queue, "record_runtime_event", lambda *a, **kw: None)
    token = {"address": MINT, "social_signal": fresh.to_dict()}
    assert q.schedule(token)
    token["social_signal"]["twitter_url"] = "https://x.com/mutated-after-schedule"
    await asyncio.gather(*q._tasks)
    result = q.get_cached(MINT)
    assert result.twitter_url == "https://x.com/new_token" and result.received_at == fresh.received_at
    assert result.risk_checked_at >= result.received_at and events
    assert thread_ids and all(identity != main_thread for identity in thread_ids)
    await q.stop()


def test_expired_social_data_uses_actual_transient_wait_not_negative_label_or_order():
    signal = observed_social(received=time.time() - 1)
    token, frozen, vector = entry_inputs(signal)
    token["social_signal"]["received_at"] -= 601
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    names = {"_entry_observation_is_current", "_defer_entry_observation"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    events, requeues = [], []
    ns = {"entry_observation_problem": entry.entry_observation_problem, "DRY_RUN": True,
        "_pending_ai_vectors": {MINT: vector},
        "_research_decision": lambda *a, **kw: events.append(kw),
        "_ensure_requeue_with_stats": lambda *a, **kw: requeues.append(kw)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "run_bot.py", "exec"), ns)
    assert ns["_entry_observation_is_current"](token, frozen, stage="pre_buy", vector=vector.to_dict()) is False
    assert not ns["_pending_ai_vectors"] and events[0]["action"] == "wait" and requeues


def entry_inputs(signal):
    token = stamp_market_observation({"address": MINT, "price_usd": 1.,
        "liquidity_is_proxy": 0, "liquidity_usd_is_proxy": 0}, "jupiter")
    social.apply_social_signal_to_token(token, signal)
    frozen = entry.freeze_entry_social_observation(token, entry.freeze_entry_observation(token, paper=True))
    vector = build_feature_vector(token)
    return token, frozen, vector


@pytest.mark.parametrize("kind", ["changed_profile", "changed_vector", "expired", "same_mint_new_response"])
def test_current_social_input_and_vector_are_rechecked_before_order(monkeypatch, kind):
    signal = observed_social(received=time.time() - 1)
    token, frozen, vector = entry_inputs(signal)
    assert entry.entry_observation_problem(token, frozen, paper=True, vector=vector.to_dict()) is None
    if kind == "changed_profile":
        token["social_signal"]["twitter_url"] = "https://x.com/changed"
    elif kind == "changed_vector":
        vector["twitter_present"] = 0
    elif kind == "same_mint_new_response":
        token["social_signal"]["received_at"] += .5
    else:
        monkeypatch.setattr(social, "social_cache_ttl_s", lambda *a: .01)
        monkeypatch.setattr(entry, "social_cache_ttl_s", lambda: .01)
    assert entry.entry_observation_problem(token, frozen, paper=True, vector=vector.to_dict()) is not None


@pytest.mark.parametrize("known", [True, False])
def test_versioned_original_receipt_proof_is_detached_historical_and_vector_bound(known):
    signal = observed_social(received=time.time() - 1) if known else social.unknown_social_signal()
    token, observation, vector = entry_inputs(signal)
    observations = entry.entry_auxiliary_observations(observation)
    proof = learning.freeze_entry_features(vector, address=MINT, captured_at=dt.datetime.now(dt.timezone.utc),
        auxiliary_observations=observations)
    assert proof["version"] == learning.ENTRY_AUX_VERSION and set(proof["vector"]) == set(COLUMNS)
    observations["social"]["source"] = "modified"
    assert proof["auxiliary_observations"]["social"]["source"] == signal.source
    # Replays use original model T0, not today's cache/wall clock.
    learning.validate_entry_features(proof, address=MINT)
    altered = deepcopy(proof)
    altered["vector"]["social_ok"] = not known
    altered["payload_sha256"] = learning._hash({key: value for key, value in altered.items() if key != "payload_sha256"})
    with pytest.raises(learning.TradeLearningError):
        learning.validate_entry_features(altered, address=MINT)
    legacy = learning.freeze_entry_features(vector, address=MINT, captured_at=dt.datetime.now(dt.timezone.utc))
    assert legacy["version"] == learning.ENTRY_VERSION and "auxiliary_observations" not in legacy
    learning.validate_entry_features(legacy, address=MINT)


@pytest.mark.parametrize("kind", ["future", "expired", "wrong_mint", "extra_schema", "unsupported_version"])
def test_auxiliary_proof_cannot_introduce_future_stale_foreign_or_extra_input(kind):
    signal = observed_social(received=time.time() - 1)
    _, frozen, vector = entry_inputs(signal)
    proof = learning.freeze_entry_features(vector, address=MINT, captured_at=dt.datetime.now(dt.timezone.utc),
        auxiliary_observations=entry.entry_auxiliary_observations(frozen))
    raw = proof["auxiliary_observations"]["social"]
    if kind == "future":
        raw["received_at"] = dt.datetime.fromisoformat(proof["vector"]["timestamp"]).timestamp() + .1
    elif kind == "expired":
        raw["received_at"] -= 601
    elif kind == "wrong_mint":
        raw["address"] = OTHER
    elif kind == "extra_schema":
        proof["auxiliary_observations"]["unreviewed"] = {"signal": 1}
    else:
        proof["version"] = "frozen_entry_features_with_auxiliary_receipts_v999"
    proof["payload_sha256"] = learning._hash({key: value for key, value in proof.items() if key != "payload_sha256"})
    with pytest.raises(learning.TradeLearningError):
        learning.validate_entry_features(proof, address=MINT)
