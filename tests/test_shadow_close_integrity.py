"""Native shadow outcomes retain unknown labels and original context on IO faults."""
from copy import deepcopy
import datetime as dt
import json
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from typing import List

import pytest

from analytics import research_runtime as research


def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "METRICS_DIR", tmp_path)
    monkeypatch.setattr(research, "RESEARCH_EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(research, "RESEARCH_PORTFOLIO_PATH", tmp_path / "portfolio.json")
    monkeypatch.setattr(research, "_OPEN_SHADOWS", {})


@pytest.mark.parametrize("unknown", [None, float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("label", [None, 0])
def test_unknown_shadow_return_is_not_an_invented_negative(tmp_path, monkeypatch, unknown, label):
    paths(tmp_path, monkeypatch)
    research._OPEN_SHADOWS["unknown"] = {"opened_at": dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc), "reason": "original"}
    research.record_shadow_close("unknown", regime="pump_early", pnl_pct=unknown,
        exit_reason="TIMEOUT_NOPRICE", label=label)
    row = json.loads(research.RESEARCH_EVENTS_PATH.read_text(encoding="utf-8").splitlines()[0])
    assert row["label"] is None and row["pnl_pct"] is None


def test_failed_shadow_event_preserves_original_open_context(tmp_path, monkeypatch):
    paths(tmp_path, monkeypatch)
    context = {"opened_at": dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc), "reason": "original"}
    research._OPEN_SHADOWS["retry"] = deepcopy(context)
    monkeypatch.setattr(research, "_write_event", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic disk failure")))
    with pytest.raises(OSError, match="synthetic disk failure"):
        research.record_shadow_close("retry", regime="pump_early", pnl_pct=100., exit_reason="TRAIL", label=1)
    assert research._OPEN_SHADOWS["retry"] == context


@pytest.mark.parametrize("observed,label", [(0., 0), (-10., 0), (200., 1), (200., None)])
def test_known_close_values_override_open_or_extra_identity(tmp_path, monkeypatch, observed, label):
    paths(tmp_path, monkeypatch)
    research._OPEN_SHADOWS["canonical"] = {"address": "other", "event_type": "candidate_decision",
        "ts_utc": "2020-01-01T00:00:00Z", "label": 0, "pnl_pct": -999.,
        "reason": "original", "regime": "old", "source": "old", "shadow_kind": "old"}
    research.record_shadow_close("canonical", regime="pump_early", pnl_pct=observed,
        exit_reason="TRAIL", label=label, shadow_kind="execution",
        extra={"address": "extra-address", "label": 0, "pnl_pct": -888., "new_context": 1})
    row = json.loads(research.RESEARCH_EVENTS_PATH.read_text(encoding="utf-8").splitlines()[0])
    assert row["address"] == "canonical" and row["event_type"] == "candidate_outcome"
    assert row["pnl_pct"] == observed and row["label"] == label
    assert row["source"] == "research_shadow" and row["shadow_kind"] == "execution"
    assert row["reason"] == "original" and row["new_context"] == 1
    assert row["ts_utc"] != "2020-01-01T00:00:00Z"
    assert "canonical" not in research._OPEN_SHADOWS


def test_failed_portfolio_write_keeps_original_context(tmp_path, monkeypatch):
    paths(tmp_path, monkeypatch)
    context = {"opened_at": dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc), "reason": "original"}
    research._OPEN_SHADOWS["portfolio-fault"] = deepcopy(context)
    monkeypatch.setattr(research, "_save_portfolio", lambda *args: (_ for _ in ()).throw(OSError("portfolio IO")))
    with pytest.raises(OSError, match="portfolio IO"):
        research.record_shadow_close("portfolio-fault", regime="pump_early", pnl_pct=100., exit_reason="TRAIL", label=1)
    assert research._OPEN_SHADOWS["portfolio-fault"] == context
    assert len(research.RESEARCH_EVENTS_PATH.read_text(encoding="utf-8").splitlines()) == 1


def test_older_close_cannot_remove_replaced_open_context(tmp_path, monkeypatch):
    paths(tmp_path, monkeypatch)
    old, new = {"reason": "original"}, {"reason": "new opening"}
    research._OPEN_SHADOWS["replaced"] = old
    save = research._save_portfolio
    def replace(payload):
        save(payload)
        research._OPEN_SHADOWS["replaced"] = new
    monkeypatch.setattr(research, "_save_portfolio", replace)
    research.record_shadow_close("replaced", regime="pump_early", pnl_pct=100., exit_reason="TRAIL", label=1)
    assert research._OPEN_SHADOWS["replaced"] is new


@pytest.mark.asyncio
async def test_actual_shadow_tick_sends_unknown_label_to_features_and_journal(tmp_path):
    now = dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc)
    feature, journal = Mock(return_value=True), Mock()
    states = {"unpriced": {"opened_at": now - dt.timedelta(hours=4),
        "buy_price_usd": 1., "vec": {"address": "unpriced", "timestamp": now - dt.timedelta(hours=4)}}}
    ns = {"List": List, "SimpleNamespace": SimpleNamespace, "utc_now": lambda: now, "PROJECT_ROOT": tmp_path,
        "_shadow_positions": states, "_RESEARCH_SHADOW_USE_GECKO": True, "ML_POSITIVE_PNL_RATIO": .05,
        "_to_float": lambda value, *args: float(value) if value is not None else None,
        "exit_policy": SimpleNamespace(effective_exit_policy=lambda sd: SimpleNamespace(max_holding_h=1),
            should_take_partial=lambda *args: False, should_exit=lambda *args, **kw: None),
        "price_service": SimpleNamespace(get_price=AsyncMock(return_value=None), get_price_usd=AsyncMock(return_value=None)),
        "store_append": feature, "_stats": {"appended_shadow": 0}, "log": SimpleNamespace(debug=Mock()),
        "research_runtime": SimpleNamespace(record_shadow_close=journal, record_shadow_partial=Mock())}
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == "_tick_shadows")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "run_bot.py", "exec"), ns)
    await ns["_tick_shadows"]()
    assert feature.call_args.args[1] is None
    assert feature.call_args.kwargs["target_total_pnl_pct"] is None
    assert journal.call_args.kwargs["label"] is None and journal.call_args.kwargs["pnl_pct"] is None
    assert not states


@pytest.mark.parametrize("kind", ["parquet", "csv"])
@pytest.mark.parametrize("with_label", [True, False])
def test_runner_source_preserves_known_peak_without_fabricating_final_label(tmp_path, monkeypatch, kind, with_label):
    import pandas as pd
    from ml import train, family_training
    row = {"address": "observed-runner", "timestamp": "2026-08-01T00:00:00Z",
        "outcome_closed_at": "2026-08-02T00:00:00Z", "sample_type": "shadow_close",
        "max_pnl_pct_seen": 25000., "target_total_pnl_pct": None,
        "entry_regime": "pump_early", "entry_lane": "pump_early_sniper_research", "liquidity_usd": 50000.,
        "age_minutes": 3., "market_cap_usd": 10000., "price_pct_5m": 100.,
        "holders": 1000, "txns_5m": 100, "score_total": 80, "dex_id": "pumpfun"}
    if with_label: row["label"] = None
    frame = pd.DataFrame([row,
        {**row, "address": "unknown-peak", "max_pnl_pct_seen": None},
        {**row, "address": "policy-not-outcome", "sample_type": "policy_reject"},
        {**row, "address": "invalid-financial", "outcome_return_basis": "declared-without-proof"}])
    path = tmp_path / ("features_202608." + kind)
    if kind == "parquet": frame.to_parquet(path, index=False)
    else: frame.to_csv(path, index=False)
    monkeypatch.setattr(train, "DATA_DIR", tmp_path)
    # Explicit fixture scope, not operator configuration or a relaxed filter.
    monkeypatch.setattr(train, "CFG", SimpleNamespace(
        ML_TRAIN_ENTRY_LANE_ALLOWLIST="pump_early_sniper_research",
        ML_TRAIN_DEX_ALLOWLIST="pumpfun", ML_TRAIN_ALLOW_MISSING_ENTRY_LANE=False))
    source = train._load_dataset(require_label=False)
    assert len(source) == 4 and source.label.isna().all()
    from ml.label_builder import attach_labels
    labels = attach_labels(source)
    assert labels.loc[labels.address.eq("observed-runner"), "runner_10000"].tolist() == [1]
    assert labels.ev_realized_clipped.isna().all()
    calls = []
    def family_source(**kwargs):
        calls.append(kwargs)
        return train._load_dataset(**kwargs)
    monkeypatch.setattr(family_training, "_load_dataset", family_source)
    monkeypatch.setattr(family_training, "PROJECT_ROOT", tmp_path)
    family = family_training.load_training_frame()
    assert calls == [{"require_label": False}]
    assert family.address.tolist() == ["observed-runner", "unknown-peak"]
    assert family.label.isna().all() and family.ev_realized_clipped.isna().all()
    assert family.loc[family.address.eq("observed-runner"), "runner_10000"].tolist() == [1]
    assert family.loc[family.address.eq("unknown-peak"), "runner_10000"].isna().all()
    assert family.timestamp.eq(pd.Timestamp(row["timestamp"])).all()
    if with_label:
        assert train._load_dataset().empty
    else:
        with pytest.raises(ValueError, match="label"): train._load_dataset()
