from pathlib import Path

import pandas as pd

from tools.audit_runner_population import profile_runner_population, runner_population_sources


def test_profile_counts_rows_and_distinct_tokens_without_inventing_extreme_support():
    times = pd.date_range("2026-07-13", periods=4, freq="10min", tz="UTC")
    frame = pd.DataFrame({"address": ["A", "A", "B", "C"], "timestamp": times,
        "ts": times + pd.Timedelta(minutes=1), "runner_1000": [1, 1, 0, None],
        "runner_10000": [None, None, None, None]})
    report = profile_runner_population(frame, as_of="2026-10-08T00:00:00Z")
    assert report["settled_rows"] == 4 and report["distinct_tokens"] == 3
    assert report["duplicate_key_rows"] == 0
    extreme = report["target_counts"]["runner_1000"]
    assert extreme == {"observed": 3, "positive_rows": 2, "positive_tokens": 1,
                       "unknown_or_invalid_rows": 1, "positive_rate": 2 / 3}
    assert report["target_counts"]["runner_10000"]["positive_rate"] is None
    assert report["checked_estimated_net_rows"] == report["current_auxiliary_receipt_rows"] == 0
    assert report["daily"][0]["runner_1000_positive_rows"] == 2


def test_profile_excludes_future_and_backwards_timing_and_records_duplicate_grain():
    frame = pd.DataFrame({"address": ["A"] * 5,
        "timestamp": pd.to_datetime(["2026-07-13T00:00:00Z"] * 4 + ["2026-10-10T00:00:00Z"], utc=True),
        "ts": pd.to_datetime(["2026-07-13T00:01:00Z", "2026-07-13T00:01:00Z", None,
                              "2026-07-12T00:00:00Z", "2026-10-10T00:01:00Z"], utc=True),
        "runner_1000": [1] * 5})
    report = profile_runner_population(frame, as_of="2026-10-08T00:00:00Z")
    assert report["settled_rows"] == 2 and report["excluded_invalid_timing_or_identity_rows"] == 3
    assert report["duplicate_key_rows"] == 2
    assert report["target_counts"]["runner_1000"]["positive_tokens"] == 1


def test_profile_empty_and_invalid_labels_remain_unknown():
    empty = profile_runner_population(pd.DataFrame(), as_of="2026-10-08T00:00:00Z")
    assert empty["settled_rows"] == 0 and empty["daily"] == [] and empty["decision_end_utc"] is None
    frame = pd.DataFrame({"address": ["A"], "timestamp": ["2026-07-13T00:00:00Z"],
                          "ts": ["2026-07-13T00:01:00Z"], "runner_1000": [3]})
    report = profile_runner_population(frame, as_of="2026-10-08T00:00:00Z")
    assert report["target_counts"]["runner_1000"]["observed"] == 0
    assert report["target_counts"]["runner_1000"]["unknown_or_invalid_rows"] == 1


def test_source_hashes_ignore_missing_files_and_do_not_include_absolute_paths(tmp_path):
    path = tmp_path / "data" / "features" / "features_202607.parquet"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"source fixture")
    sources = runner_population_sources(tmp_path)
    assert len(sources) == 1 and sources[0]["file"] == "data/features/features_202607.parquet"
    assert sources[0]["bytes"] == 14 and len(sources[0]["sha256"]) == 64
