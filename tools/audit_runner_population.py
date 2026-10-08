"""Read-only runner evidence profile; no training, activation or trading."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import pandas as pd

from features.auxiliary_semantics import population_proof, prepare_training_frame
from ml.financial_targets import checked_financial_frame
from ml.label_builder import RUNNER_THRESHOLDS
from ml.temporal_validation import temporal_eligibility


def profile_runner_population(frame: pd.DataFrame, *, as_of: Any) -> dict[str, Any]:
    """Count original eligible feature rows, not orders or realizable profits."""
    valid, times, available, identities = temporal_eligibility(frame, as_of=as_of)
    settled = frame.loc[valid].copy()
    settled_ids = identities.loc[valid]
    keys = pd.DataFrame({"token": settled_ids, "decision": times.loc[valid]})
    target_counts = {}
    for threshold in RUNNER_THRESHOLDS:
        name = f"runner_{threshold}"
        values = pd.to_numeric(settled.get(name, pd.Series(index=settled.index, dtype=float)), errors="coerce")
        known = values.isin([0, 1])
        positive = known & values.eq(1)
        observed = int(known.sum())
        target_counts[name] = {
            "observed": observed, "positive_rows": int(positive.sum()),
            "positive_tokens": int(settled_ids.loc[positive].nunique()),
            "unknown_or_invalid_rows": int((~known).sum()),
            "positive_rate": float(positive.sum() / observed) if observed else None,
        }
    daily = []
    days = times.loc[valid].dt.strftime("%Y-%m-%d")
    for day in sorted(days.unique()):
        mask = days.eq(day)
        row = {"day_utc": day, "eligible_rows": int(mask.sum()),
               "distinct_tokens": int(settled_ids.loc[mask].nunique())}
        for threshold in RUNNER_THRESHOLDS:
            values = pd.to_numeric(settled.get(f"runner_{threshold}", pd.Series(index=settled.index, dtype=float)), errors="coerce")
            row[f"runner_{threshold}_positive_rows"] = int((mask & values.eq(1)).sum())
            row[f"runner_{threshold}_observed_rows"] = int((mask & values.isin([0, 1])).sum())
        daily.append(row)
    financial, financial_report = checked_financial_frame(settled)
    _, semantics = prepare_training_frame(settled)
    original = population_proof(settled)
    return {
        "grain": "eligible outcome feature row identified by case-sensitive token and T0 decision time",
        "source_rows": len(frame), "columns": len(frame.columns),
        "settled_rows": len(settled), "distinct_tokens": int(settled_ids.nunique()),
        "excluded_invalid_timing_or_identity_rows": int((~valid).sum()),
        "duplicate_key_rows": int(keys.duplicated(keep=False).sum()),
        "decision_start_utc": times.loc[valid].min().isoformat() if valid.any() else None,
        "decision_end_utc": times.loc[valid].max().isoformat() if valid.any() else None,
        "last_outcome_available_utc": available.loc[valid].max().isoformat() if valid.any() else None,
        "target_join": frame.attrs.get("outcome_target_join"),
        "target_counts": target_counts, "daily": daily,
        "checked_estimated_net_rows": len(financial), "financial_population": financial_report,
        "current_auxiliary_receipt_rows": original["current_rows"],
        "auxiliary_training_population": semantics,
        "as_of_utc": pd.to_datetime(as_of, utc=True).isoformat(),
        "caveats": [
            "Overlapping runner thresholds must not be summed.",
            "Observed shadow price peaks are not orders, executable proceeds or costed returns.",
            "Repeated observations of one token do not provide independent extreme-event support.",
            "Historic evidence does not establish current opportunity coverage or future profitability.",
            "Unknown or unsupported event labels are not negative outcomes.",
        ],
    }


def runner_population_sources(root: Path) -> list[dict[str, Any]]:
    paths = sorted((root / "data" / "features").glob("features_*.parquet"))
    paths.append(root / "data" / "metrics" / "candidate_outcomes.jsonl")
    sources = []
    for path in paths:
        if not path.is_file():
            continue
        digest = sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        stat = path.stat()
        sources.append({"file": path.relative_to(root).as_posix(), "sha256": digest.hexdigest(),
                        "bytes": stat.st_size,
                        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
    return sources
