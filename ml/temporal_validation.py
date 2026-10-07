"""Expanding, token-disjoint validation with label-availability purging."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def temporal_eligibility(frame: pd.DataFrame, *, as_of: Any = None):
    """Known, settled observations only; missing time is never 'now'."""
    times = pd.to_datetime(frame.get("timestamp", pd.Series(pd.NaT, index=frame.index)), utc=True, errors="coerce")
    available = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
    for column in ("outcome_closed_at", "closed_at", "ts"):
        if column in frame:
            available = available.fillna(pd.to_datetime(frame[column], utc=True, errors="coerce"))
    identities = pd.Series(pd.NA, index=frame.index, dtype="string")
    for column in ("mint", "address", "token_address"):
        if column in frame:
            identities = identities.fillna(frame[column].astype("string").str.strip().replace("", pd.NA))
    cutoff = pd.Timestamp.now(tz="UTC") if as_of is None else pd.to_datetime(as_of, utc=True)
    valid = times.notna() & available.notna() & identities.notna() & (available >= times)
    valid &= (times <= cutoff) & (available <= cutoff)
    return valid, times, available, identities


def purged_temporal_windows(
    frame: pd.DataFrame,
    *,
    splits: int = 3,
    min_train_rows: int = 20,
    embargo_seconds: float = 60.0,
    as_of: Any = None,
) -> tuple[list[tuple[np.ndarray, np.ndarray]], dict[str, Any]]:
    """Return positional indices, never inventing missing event/label times.

    A train label must have become available *before* the validation decision.
    All observations of a validation token are excluded from that fold's train.
    Equal decision timestamps always remain in the same chunk.
    """
    valid, times, available, identities = temporal_eligibility(frame, as_of=as_of)
    meta: dict[str, Any] = {
        "mode": "purged_token_walk_forward",
        "rows": int(len(frame)),
        "eligible_rows": int(valid.sum()),
        "excluded_missing_or_invalid_timing_or_identity": int((~valid).sum()),
        "embargo_seconds": float(embargo_seconds),
        "folds": [],
    }
    unique_times = np.asarray(sorted(times[valid].unique()))
    if len(unique_times) < max(4, int(splits) + 1):
        meta["reason"] = "insufficient_distinct_decision_times"
        return [], meta
    chunks = [chunk for chunk in np.array_split(unique_times, max(2, int(splits) + 1)) if len(chunk)]
    windows: list[tuple[np.ndarray, np.ndarray]] = []
    for chunk in chunks[1:]:
        start = pd.Timestamp(chunk[0])
        test_mask = valid & times.isin(chunk)
        test_tokens = set(identities[test_mask])
        time_mask = valid & (times < start)
        mature = available < (start - pd.Timedelta(seconds=max(0.0, embargo_seconds)))
        train_mask = time_mask & mature & ~identities.isin(test_tokens)
        train = np.flatnonzero(train_mask.to_numpy())
        test = np.flatnonzero(test_mask.to_numpy())
        fold = {
            "train_rows": int(len(train)), "test_rows": int(len(test)),
            "test_start": start.isoformat(), "test_end": pd.Timestamp(chunk[-1]).isoformat(),
            "train_label_latest": available[train_mask].max().isoformat() if len(train) else None,
            "purged_unsettled_or_embargo": int((time_mask & ~mature).sum()),
            "purged_shared_tokens": int((time_mask & mature & identities.isin(test_tokens)).sum()),
            "used": bool(len(train) >= min_train_rows and len(test)),
        }
        meta["folds"].append(fold)
        if fold["used"]:
            windows.append((train, test))
    if not windows:
        meta["reason"] = "no_mature_token_disjoint_training_window"
    return windows, meta


__all__ = ["purged_temporal_windows", "temporal_eligibility"]
