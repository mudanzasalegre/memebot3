from __future__ import annotations

from typing import Any

import pandas as pd


def temporal_windows(frame: pd.DataFrame, *, timestamp_col: str = "timestamp", splits: int = 3) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    if frame.empty or timestamp_col not in frame.columns:
        return []
    df = frame.copy()
    df[timestamp_col] = pd.to_datetime(df[timestamp_col], utc=True, errors="coerce")
    df = df.dropna(subset=[timestamp_col]).sort_values(timestamp_col)
    if len(df) < max(2, splits + 1):
        return []
    chunks = [chunk for chunk in pd.array_split(df, max(2, splits + 1)) if not chunk.empty]
    windows = []
    for idx in range(1, len(chunks)):
        train = pd.concat(chunks[:idx], ignore_index=True)
        test = chunks[idx].copy()
        if not train.empty and not test.empty:
            windows.append((train, test))
    return windows


def walk_forward_report(frame: pd.DataFrame, *, timestamp_col: str = "timestamp", splits: int = 3) -> dict[str, Any]:
    windows = temporal_windows(frame, timestamp_col=timestamp_col, splits=splits)
    return {
        "windows": len(windows),
        "rows": int(len(frame)),
        "splits": [
            {
                "train_rows": int(len(train)),
                "test_rows": int(len(test)),
                "train_start": str(train[timestamp_col].min()) if timestamp_col in train else None,
                "test_start": str(test[timestamp_col].min()) if timestamp_col in test else None,
            }
            for train, test in windows
        ],
    }


__all__ = ["temporal_windows", "walk_forward_report"]
