"""One authoritative feature file per partition, shared by native consumers.

Parquet is the native writer's source; its same-stem CSV is an exported replica.
A Parquet in one partition must not hide CSV-only partitions elsewhere. Selection
does not merge rows, authenticate clocks, validate labels or approve training.
"""
from __future__ import annotations

from pathlib import Path


def feature_partition_files(directory: Path) -> list[Path]:
    directory = Path(directory)
    partitions = {path.stem: path for path in directory.glob("features_*.csv")
                  if path.is_file()}
    partitions.update({path.stem: path for path in directory.glob("features_*.parquet")
                       if path.is_file()})
    return [partitions[stem] for stem in sorted(partitions)]
