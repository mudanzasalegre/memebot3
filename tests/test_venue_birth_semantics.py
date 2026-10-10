"""Native synthetic source-grain checks, not birth/access/profit acceptance."""
from copy import deepcopy
import datetime as dt

import pandas as pd
import pytest

from analytics import token_time
from fetcher import dexscreener as dex, geckoterminal as gt, birdeye
from runtime.entry_observation import prepare_entry_candidate
from utils.data_utils import sanitize_token_data
from utils.market_observation import stamp_market_observation

NOW = dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc)
MINT = "So11111111111111111111111111111111111111112"
OTHER = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAIR = "11111111111111111111111111111111"
VENUE_FIELDS = ["pairCreatedAt", "pair_created_at", "pairCreatedAtMs", "pool_created_at", "listedAt"]


def pair(**changes):
    return {"chainId":"solana", "baseToken":{"address":MINT}, "pairAddress":PAIR,
            "priceUsd":"2", "liquidity":{"usd":5000}, **changes}


@pytest.mark.parametrize("field", VENUE_FIELDS)
def test_venue_or_listing_clock_is_not_birth_in_common_age(field):
    value = NOW-dt.timedelta(minutes=3)
    row = {field:value, "first_seen_at":NOW-dt.timedelta(minutes=1)}
    assert token_time.compute_age_minutes(row, now=NOW) is None
    assert token_time.compute_queue_age_minutes(row, now=NOW) == 1


@pytest.mark.parametrize("field", VENUE_FIELDS)
def test_sanitizer_cannot_resurrect_venue_clock_as_created_at(field):
    row = sanitize_token_data({"address":MINT, field:NOW-dt.timedelta(minutes=3)})
    assert row["created_at"] is None and row["age_minutes"] is None


@pytest.mark.parametrize("field", VENUE_FIELDS)
def test_historical_and_model_projection_keep_venue_only_birth_missing(field):
    from features.builder import build_feature_vector
    from features.numeric_encoding import PREFIX
    from ml.feature_matrix import coerce_feature_frame
    original = {"address":MINT, field:NOW-dt.timedelta(minutes=3), "decision_at":NOW}
    before = deepcopy(original)
    historical = token_time.historical_age_snapshot(original)
    assert historical["age_minutes"] is None and original == before
    vector = build_feature_vector(original, now=NOW)
    matrix = coerce_feature_frame(vector.to_frame().T, ["age_minutes", PREFIX+"age_minutes"])
    assert pd.isna(vector["age_minutes"]) and matrix.iloc[0][PREFIX+"age_minutes"] == 1


def test_real_birth_and_measured_zero_remain_distinct_from_venue_clock():
    row = {"created_at":NOW-dt.timedelta(days=100), "pairCreatedAt":NOW-dt.timedelta(minutes=3)}
    assert token_time.compute_age_minutes(row, now=NOW) == 144000
    assert token_time.compute_age_minutes({"age_minutes":0,"pairCreatedAt":NOW-dt.timedelta(days=100)},now=NOW) == 0


@pytest.mark.parametrize("field", ["listedAt","createdAt","created","created_at","age_minutes","age_min","token_age_min"])
def test_native_pair_normalization_never_promotes_untyped_birth_alias(field):
    raw = pair(pairCreatedAt=int((NOW-dt.timedelta(days=100)).timestamp())*1000,
               **{field:2 if "age" in field else NOW-dt.timedelta(minutes=3)})
    original = deepcopy(raw)
    row = dex._norm_from_pair(raw)
    assert row["created_at"] is None and token_time.compute_age_minutes(row,now=NOW) is None
    assert row["pair_created_at"] == NOW-dt.timedelta(days=100)
    assert row["venue_clock_metadata"][field] == raw[field]
    assert raw == original


@pytest.mark.parametrize("bad", [True,False,0,-1,float("inf"),float("nan"),"bad",{},[]])
def test_invalid_pair_clock_stays_unknown_without_listing_fallback(bad):
    row=dex._norm_from_pair(pair(pairCreatedAt=bad,listedAt=NOW-dt.timedelta(minutes=3)))
    assert row["pair_created_at"] is None and row["created_at"] is None


@pytest.mark.parametrize("changes", [{"txns":[1]},{"txns":{"m5":[1]}},{"txns":True},
    {"priceChange":[1]},{"priceChange":True},{"volumeChange":[1]},{"dex":[1]}])
def test_malformed_optional_pair_nodes_are_isolated_without_dropping_market_identity(changes):
    row=dex._norm_from_pair(pair(**changes))
    assert row["address"]==MINT and row["price_usd"]==2 and row["liquidity_usd"]==5000
    assert row["created_at"] is None and token_time.compute_age_minutes(row,now=NOW) is None


@pytest.mark.parametrize("field", ["pool_created_at","created_at","listed_at","launched_at"])
def test_gecko_token_attributes_do_not_infer_mint_birth(field):
    raw={field:(NOW-dt.timedelta(days=100)).isoformat(),"price_usd":"2","total_reserve_in_usd":"5000"}
    row=gt._normalize_attributes(MINT,raw)
    assert row["created_at"] is None and token_time.compute_age_minutes(row,now=NOW) is None
    if field=="pool_created_at": assert row["pair_created_at"]==NOW-dt.timedelta(days=100)


@pytest.mark.parametrize("field,value", [("createdAt",(NOW-dt.timedelta(days=100)).isoformat()),
    ("createUnixTime",(NOW-dt.timedelta(days=100)).timestamp())])
def test_birdeye_pool_clock_is_not_its_base_mint_birth(field,value):
    row=birdeye._normalize_pool_payload(PAIR,{"baseMint":MINT,field:value,"priceUsd":"2","tvlUsd":5000})
    assert row["created_at"] is None and token_time.compute_age_minutes(row,now=NOW) is None
    assert row["pair_created_at"]==NOW-dt.timedelta(days=100)


def test_fresh_pair_market_does_not_rejuvenate_original_known_discovery_birth():
    snapshot=stamp_market_observation(dex._norm_from_pair(pair(pairCreatedAt=NOW-dt.timedelta(minutes=3))),"dexscreener")
    queued={"address":MINT,"created_at":NOW-dt.timedelta(days=100),"first_seen_at":NOW-dt.timedelta(minutes=1)}
    before=deepcopy(queued)
    row=prepare_entry_candidate(queued,snapshot)
    assert token_time.compute_age_minutes(row,now=NOW)==144000
    assert token_time.compute_queue_age_minutes(row,now=NOW)==1
    assert row["pair_created_at"]==NOW-dt.timedelta(minutes=3) and queued==before


@pytest.mark.parametrize("chain", [None,"ethereum","sol",True,{}])
def test_native_pair_selection_never_treats_unknown_or_foreign_chain_as_solana(chain):
    assert dex._matching_pairs([pair(chainId=chain)],MINT)==[]
    assert dex._pick_best_pair([pair(chainId=chain)]) is None


def test_native_profile_or_pair_alias_cannot_substitute_a_different_base_mint():
    assert dex._matching_pairs([pair(baseToken={"address":OTHER},tokenAddress=MINT)],MINT)==[]


@pytest.mark.parametrize("chain", ["Solana"," solana "])
def test_canonical_identity_can_be_trimmed_without_losing_a_valid_solana_market(chain):
    raw=pair(chainId=chain,baseToken={"address":" "+MINT+" "})
    assert dex._matching_pairs([raw],MINT)==[raw] and dex._pick_best_pair([raw])==raw


@pytest.fixture
def native_http(monkeypatch):
    class Response:
        status=200
        def __init__(self,payload):self.payload=payload
        async def __aenter__(self):return self
        async def __aexit__(self,*args): client.responses_closed+=1
        def raise_for_status(self):pass
        async def json(self):return deepcopy(self.payload)
    class Session:
        def __init__(self):self.calls=[];self.payloads=[];self.responses_closed=0;self.closed=0
        async def __aenter__(self):return self
        async def __aexit__(self,*args):self.closed+=1
        def get(self,url,**kwargs):
            self.calls.append((url,kwargs));return Response(self.payloads.pop(0))
    client=Session()
    monkeypatch.setattr(dex.aiohttp,"ClientSession",lambda:client)
    monkeypatch.setattr(dex,"cache_get",lambda *a:None)
    monkeypatch.setattr(dex,"cache_set",lambda *a,**k:None)
    monkeypatch.setattr(dex,"cache_delete",lambda *a:None)
    monkeypatch.setattr(dex,"_fail_count",{})
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope", ["list","pairs"])
async def test_native_documented_token_pair_route_accepts_actual_collection_grain(native_http,envelope):
    native_http.payloads=[[pair()] if envelope=="list" else {"pairs":[pair()]},None,None]
    row=await dex.get_pair(MINT,force_refresh=True)
    assert row["address"]==MINT
    assert native_http.calls[0][0]==dex._u("token-pairs/v1/solana",MINT)
    assert len(native_http.calls)==native_http.responses_closed==1 and native_http.closed==1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None,True,{},"invalid","0x123"])
async def test_invalid_input_cannot_start_native_provider_http(native_http,bad):
    native_http.payloads=[None,None,None]
    assert await dex.get_pair(bad,force_refresh=True) is None
    assert native_http.calls==[]


@pytest.mark.asyncio
async def test_native_pair_lookup_keeps_explicit_pair_to_base_mapping(native_http):
    native_http.payloads=[[],{"pairs":[pair()]},None]
    row=await dex.get_pair(PAIR,force_refresh=True)
    assert row["address"]==MINT and row["pair_address"]==PAIR
    assert len(native_http.calls)==native_http.responses_closed==2 and native_http.closed==1


@pytest.mark.asyncio
async def test_native_unknown_primary_shape_is_isolated_before_pair_fallback(native_http):
    native_http.payloads=[{"status":"unknown"},{"pairs":[pair()]},None]
    row=await dex.get_pair(PAIR,force_refresh=True)
    assert row["address"]==MINT and len(native_http.calls)==2


@pytest.mark.parametrize("value", [True,False,0,-1,float("nan"),float("inf"),"bad",[],{},NOW+dt.timedelta(days=1)])
def test_shared_event_diagnostic_does_not_invent_or_accept_future_clocks(value):
    assert token_time.parse_event_clock(value,now=NOW) is None


@pytest.mark.parametrize("factor", [1,1000,1000000,1000000000])
def test_shared_event_diagnostic_retains_epoch_units_without_birth_claim(factor):
    event=NOW-dt.timedelta(days=100)
    assert token_time.parse_event_clock(int(event.timestamp())*factor,now=NOW)==event


@pytest.mark.parametrize("dex_base", ["https://api.dexscreener.com","https://api.dexscreener.com/latest"])
def test_documented_routes_do_not_duplicate_configured_latest_prefix(monkeypatch,dex_base):
    monkeypatch.setattr(dex,"DEX",dex_base)
    assert dex._u("token-pairs/v1/solana",MINT)==f"https://api.dexscreener.com/token-pairs/v1/solana/{MINT}"
    assert dex._u("latest/dex/pairs/solana",PAIR)==f"https://api.dexscreener.com/latest/dex/pairs/solana/{PAIR}"


def test_original_venue_clock_metadata_is_detached_and_scoped_to_its_pair():
    raw=pair(createdAt={"nested":[1]},pairCreatedAt=NOW-dt.timedelta(minutes=3))
    row=dex._norm_from_pair(raw)
    record=row["venue_clock"]
    assert record=={"kind":"pair","source":"dexscreener","pair_address":PAIR,
        "created_at":NOW-dt.timedelta(minutes=3),"basis":"venue_event_not_mint_birth"}
    row["venue_clock_metadata"]["createdAt"]["nested"].append(2)
    assert raw["createdAt"]=={"nested":[1]}


@pytest.mark.parametrize("primary_pair", [None,PAIR,OTHER])
def test_native_price_service_moves_venue_identity_and_clock_atomically(primary_pair):
    from utils.price_service import _merge_market_fields
    secondary=dex._norm_from_pair(pair(pairCreatedAt=NOW-dt.timedelta(minutes=3)))
    primary={"address":MINT,"price_usd":2,"created_at":NOW-dt.timedelta(days=100),"pair_address":primary_pair}
    before=deepcopy(primary)
    row=_merge_market_fields(primary,secondary,"dexscreener")
    assert row["created_at"]==before["created_at"] and primary==before
    if primary_pair==OTHER:
        assert "venue_clock" not in row and "pair_created_at" not in row
    else:
        assert row["venue_clock"]["pair_address"]==row["pair_address"]==PAIR
        assert row["pair_created_at"]==NOW-dt.timedelta(minutes=3)


@pytest.mark.parametrize("feature", ["age_minutes","queue_age_minutes"])
@pytest.mark.parametrize("encoded", [False,True])
def test_age_using_model_schema_requires_current_clock_semantics_without_rewriting_artifacts(feature,encoded):
    from features.numeric_encoding import numeric_encoding_schema,checked_numeric_schema,PREFIX
    names=[feature]+([PREFIX+feature] if encoded else [])
    schema=numeric_encoding_schema(names)
    assert schema["token_clock_semantics"]==token_time.AGE_SEMANTICS_VERSION
    assert checked_numeric_schema({"numeric_encoding":schema},names)
    obsolete=deepcopy(schema);obsolete.pop("token_clock_semantics")
    assert not checked_numeric_schema({"numeric_encoding":obsolete},names)
    assert not checked_numeric_schema({"numeric_encoding":{**schema,"token_clock_semantics":"venue_is_birth"}},names)


def test_unrelated_numeric_model_contract_does_not_gain_an_age_dependency():
    from features.numeric_encoding import numeric_encoding_schema,PREFIX
    assert "token_clock_semantics" not in numeric_encoding_schema(["liquidity_usd",PREFIX+"liquidity_usd"])


def test_actual_runner_pipeline_cannot_reuse_prior_no_change_fingerprint():
    from ml.runner_advisory_learning import PIPELINE_VERSION
    assert PIPELINE_VERSION>=12


@pytest.mark.parametrize("encoded", [False,True])
def test_actual_native_model_reader_rejects_obsolete_age_generation_before_deserialization(tmp_path,monkeypatch,encoded):
    from hashlib import sha256
    import json
    import analytics.model_runtime_common as runtime
    from features.numeric_encoding import numeric_encoding_schema,PREFIX
    names=["age_minutes"]+([PREFIX+"age_minutes"] if encoded else [])
    current=numeric_encoding_schema(names)
    obsolete=deepcopy(current);obsolete.pop("token_clock_semantics")
    path=tmp_path/"runner_100.pkl"
    path.write_bytes(b"isolated-synthetic-model-only")
    metadata={"features":names,"model_sha256":sha256(path.read_bytes()).hexdigest(),"numeric_encoding":obsolete}
    path.with_suffix(".meta.json").write_text(json.dumps(metadata),encoding="utf-8")
    original=path.with_suffix(".meta.json").read_bytes()
    monkeypatch.setattr(runtime.joblib,"load",lambda *a,**kw:pytest.fail("Obsolete clock model reached deserialization"))
    runtime.invalidate_model_cache(path)
    assert runtime._load_unscoped(path,require_temporal_validation=False)[0] is None
    assert path.with_suffix(".meta.json").read_bytes()==original
