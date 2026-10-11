"""Native synthetic calibrated bundles; no operator artifacts or fake approvals.

The retained predecessor is simulated by removing only the newly introduced
valuation metadata from an independently approved fixture and rebinding its
metadata digest. Financial/probability/cohort evidence is rechecked natively.
This is not a historical operator model or historical profitability claim.
"""
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
import json
from types import SimpleNamespace

import pandas as pd


def population(n=240, *, start="2026-09-01", prefix="RetirementFit"):
    from features.builder import build_feature_vector
    from features.auxiliary_semantics import input_frame
    from net_financial_fixtures import mint_for, net_frame
    from utils.market_observation import stamp_market_observation
    rows = []
    for i, at in enumerate(pd.date_range(start, periods=n, freq="10min", tz="UTC")):
        token = stamp_market_observation({"address": mint_for(f"{prefix}{i}"),
            "market_cap_usd": 12000., "fdv_usd": 99999., "liquidity_usd": 20000.,
            "price_pct_5m": 80. if i % 2 else 5., "entry_regime": "pump_early",
            "entry_lane": "pump_early_pumpswap_profit", "dex_id": "pumpswap"},
            "dexscreener", received_at=at.timestamp()-1)
        row = input_frame(build_feature_vector(token, now=at)).iloc[0].to_dict()
        row.update(ts=at+pd.Timedelta(minutes=2), target_total_pnl_pct=200. if i % 2 else -30.)
        rows.append(row)
    return net_frame(pd.DataFrame(rows))


@lru_cache(maxsize=2)
def strong_parts(encoded=False):
    from ml import train
    from ml.entry_probability import probability_metadata
    from ml.financial_targets import checked_financial_frame
    from ml.primary_champion import training_provenance
    from features.numeric_encoding import PREFIX, numeric_encoding_schema
    from features.valuation_semantics import population_proof
    data, financial = checked_financial_frame(population())
    names = ["price_pct_5m", "market_cap_usd"]
    if encoded:
        names += [PREFIX+name for name in names.copy()]
    evaluation = train._evaluate_candidate(name="synthetic_retirement", model_family="isolated_logreg",
        builder=train._fit_logreg_calibrated, x_cols=names, use_forward=True,
        tr_df=data.iloc[:180], te_df=data.iloc[180:])
    model = train._fit_logreg_calibrated(data, names)
    meta = {**probability_metadata(model, evaluation.probability_evaluation), "features": names,
        "rows": len(data), "activation_ready": True, "financial_training": financial,
        "training_provenance": training_provenance(data),
        "validation_split": {"label_availability_purged": True}, "feature_set_hash": "synthetic",
        "numeric_encoding": numeric_encoding_schema(names), "valuation_training": population_proof(data),
        "threshold_result": {"picked": .73, "activation_ready": True}}
    return model, meta


def artifact(root, monkeypatch, *, name="current", start="2026-09-15", prefix="FreshRetirement",
             encoded=False, incumbent=None):
    from ml import model_registry as registry
    from ml.primary_champion import current_incumbent, authorize_candidate
    monkeypatch.setattr(registry, "MODELS_DIR", root/"ml"/"models")
    monkeypatch.setattr(registry, "REGISTRY_PATH", root/"ml"/"registry.json")
    monkeypatch.setattr(registry, "PROJECT_ROOT", root)
    monkeypatch.setattr(registry, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False))
    model, meta = deepcopy(strong_parts(encoded))
    candidate = registry.write_candidate(model=model, meta=meta,
        thresholds={"global": {"threshold": .73, "activation_ready": True}, "by_lane": {}}, model_id=name)
    alias = root/"ml"/"model.pkl"
    state = incumbent if incumbent is not None else current_incumbent(
        registry_path=registry.REGISTRY_PATH, models_dir=registry.MODELS_DIR, model_alias=alias)
    cohort = population(60, start=start, prefix=prefix)
    approval = authorize_candidate(candidate, cohort, incumbent=state)
    return registry, candidate, approval, cohort, alias


def predecessor_schema(names):
    """Exact pre-valuation shape; not a generic unknown-schema exemption."""
    from features.numeric_encoding import numeric_encoding_schema
    current = deepcopy(numeric_encoding_schema(names))
    current.pop("valuation_semantics")
    return current if current.get("encoded_features") or "token_clock_semantics" in current else None


def retained_bundle(root, monkeypatch, *, encoded=False):
    from ml.primary_activation import write_bundle, commit_selection
    from ml.primary_champion import supported_approval
    registry, candidate, approval, cohort, alias = artifact(root, monkeypatch, name="retained",
        start="2026-09-10", prefix="RetainedSelection", encoded=encoded)
    assert approval["accepted"]
    model_bytes = candidate.model_path.read_bytes()
    meta = json.loads(candidate.meta_path.read_bytes())
    meta["numeric_encoding"] = predecessor_schema(meta["features"])
    meta.pop("valuation_training")
    encode = lambda value: json.dumps(value, sort_keys=True, allow_nan=False).encode()
    meta_bytes = encode(meta)
    approval["candidate_meta_sha256"] = sha256(meta_bytes).hexdigest()
    assert supported_approval(approval, model_bytes, meta_bytes)
    payloads = {"model.pkl": model_bytes, "model.meta.json": meta_bytes,
        "threshold.json": encode(meta["threshold_result"]),
        "thresholds.by_lane.json": encode(meta["thresholds_by_lane"]), "acceptance.json": encode(approval)}
    reference = write_bundle(payloads, registry_path=registry.REGISTRY_PATH,
        models_dir=registry.MODELS_DIR, model_id=candidate.model_id)
    selection = commit_selection({}, registry_path=registry.REGISTRY_PATH,
        active=reference, previous=None, model_alias=alias)
    return registry, alias, selection, meta, approval, cohort
