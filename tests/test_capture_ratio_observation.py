"""Synthetic outcome definitions; no market, model fit or profitability claim."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.label_builder import attach_labels, build_labels


@pytest.mark.parametrize("realized,peak", [
    (None, 25000.), (np.nan, 25000.), (pd.NA, 25000.),
    ("unavailable", 25000.), (np.inf, 25000.), (-np.inf, 25000.),
    (25., None), (25., np.nan), (25., pd.NA), (25., "unavailable"),
    (25., np.inf), (25., -np.inf), (25., 0.), (25., -100.),
    (None, None), (0., None), (0., 0.),
])
def test_unknown_or_undefined_capture_stays_unobserved(realized, peak):
    frame = pd.DataFrame({"target_total_pnl_pct": [realized], "max_pnl_pct_seen": [peak]}, index=[73])
    for labels in (build_labels(frame), attach_labels(frame)):
        assert labels.index.equals(frame.index)
        assert labels.capture_ratio.isna().all()


@pytest.mark.parametrize("fields", [{}, {"target_total_pnl_pct": 0.}, {"max_pnl_pct_seen": 25000.}])
def test_missing_outcome_columns_do_not_invent_zero_capture(fields):
    frame = pd.DataFrame([fields], index=[29])
    assert build_labels(frame).capture_ratio.isna().all()


@pytest.mark.parametrize("realized,peak,expected", [
    (0., 25000., 0.), (-0., 100., 0.), (-50., 100., -.5),
    (25., 100., .25), (50000., 100000., .5),
    (100000000., 200000000., .5), (300., 100., 3.),
])
def test_actual_signed_zero_and_uncapped_capture_remain_numeric(realized, peak, expected):
    frame = pd.DataFrame({"target_total_pnl_pct": [realized], "max_pnl_pct_seen": [peak]})
    labels = attach_labels(frame)
    assert labels.capture_ratio.iloc[0] == pytest.approx(expected)
    assert labels.target_total_pnl_pct.iloc[0] == realized
    assert labels.max_pnl_pct_seen.iloc[0] == peak


def test_column_precedence_preserves_observed_zero_and_uses_finite_fallback():
    frame = pd.DataFrame({"realized_pnl_pct": [0., np.inf, None],
        "target_total_pnl_pct": [99., -50., 25.], "max_pnl_seen": [100., None, np.inf],
        "max_pnl_pct_seen": [200., 100., 25000.]}, index=[8, 3, 19])
    before = frame.copy(deep=True)
    labels = attach_labels(frame)
    assert labels.capture_ratio.tolist() == pytest.approx([0., -.5, .001])
    pd.testing.assert_frame_equal(frame, before)


def test_nonfinite_arithmetic_is_unobserved_not_zero():
    frame = pd.DataFrame({"target_total_pnl_pct": [1e308], "max_pnl_pct_seen": [1e-308]})
    assert build_labels(frame).capture_ratio.isna().all()


def test_family_loading_preserves_missingness_and_real_zero_without_fitting():
    from ml.family_training import load_training_frame
    frame = pd.DataFrame({"target_total_pnl_pct": [None, 0., -50., 25.],
        "max_pnl_pct_seen": [100., 100., 100., None]}, index=[17, 4, 81, 22])
    labels = load_training_frame(frame)
    assert labels.index.equals(frame.index)
    assert labels.capture_ratio.isna().tolist() == [True, False, False, True]
    assert labels.capture_ratio.iloc[1:3].tolist() == [0., -.5]


@pytest.mark.parametrize("net", [0., 25., -50.])
def test_checked_net_return_not_gross_alias_supplies_capture_numerator(net):
    from ml.financial_targets import checked_net_return
    from net_financial_fixtures import net_frame
    frame = net_frame(pd.DataFrame([{"address": "synthetic-capture-ratio",
        "timestamp": "2026-09-01T00:00:00Z", "ts": "2026-09-01T00:02:00Z",
        "target_total_pnl_pct": net, "max_pnl_pct_seen": 25000.}]))
    assert checked_net_return(frame.iloc[0]) == pytest.approx(net)
    frame["realized_pnl_pct"] = frame["pnl_pct"] = frame["total_pnl_pct"] = 99999999.
    labels = attach_labels(frame)
    assert labels.capture_ratio.iloc[0] == pytest.approx(net / 25000.)
    assert labels.realized_pnl_pct.iloc[0] == pytest.approx(net)


def test_broken_declared_financial_proof_cannot_fall_through_to_zero_capture():
    frame = pd.DataFrame([{"outcome_return_basis": "broken", "outcome_execution_proof": "{}",
        "realized_pnl_pct": 50., "target_total_pnl_pct": 50., "max_pnl_pct_seen": 25000.}])
    labels = attach_labels(frame)
    assert labels.capture_ratio.isna().all()
    assert labels.realized_pnl_pct.isna().all() and labels.max_pnl_pct_seen.isna().all()


@pytest.mark.parametrize("format", ["csv", "parquet"])
def test_capture_missingness_survives_native_file_roundtrip(tmp_path, format):
    frame = pd.DataFrame({"address": ["synthetic-unknown", "synthetic-zero", "synthetic-loss", "synthetic-no-peak"],
        "timestamp": pd.date_range("2026-09-01", periods=4, freq="min", tz="UTC"),
        "target_total_pnl_pct": [None, 0., -50., 25.], "max_pnl_pct_seen": [100., 100., 100., None]})
    labels = attach_labels(frame)
    path = tmp_path / f"synthetic-outcomes.{format}"
    if format == "csv":
        labels.to_csv(path, index=False)
        restored = pd.read_csv(path)
    else:
        labels.to_parquet(path, index=False)
        restored = pd.read_parquet(path)
    assert restored.capture_ratio.isna().tolist() == [True, False, False, True]
    assert restored.capture_ratio.iloc[1:3].tolist() == [0., -.5]
    pd.testing.assert_series_equal(pd.to_datetime(restored.timestamp, utc=True), frame.timestamp)


def test_empty_frame_keeps_empty_numeric_capture_column():
    labels = build_labels(pd.DataFrame())
    assert labels.empty and str(labels.capture_ratio.dtype) == "float64"
