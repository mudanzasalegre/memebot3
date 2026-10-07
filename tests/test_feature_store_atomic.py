from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import features.store as store


def _row_table(address: str) -> pa.Table:
    return pa.Table.from_pydict(
        {
            "address": [address],
            "label": [1],
            "target_total_pnl_pct": [1.0],
            "sample_type": ["trade_close"],
        }
    )


def test_parquet_read_modify_write_is_serialized(tmp_path: Path) -> None:
    path = tmp_path / "features_202607.parquet"
    addresses = [f"mint-{index}" for index in range(12)]

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda address: store._write(_row_table(address), path), addresses))

    result = pq.read_table(path)
    assert result.num_rows == len(addresses)
    assert set(result.column("address").to_pylist()) == set(addresses)
    assert not path.with_name(f"{path.name}.lock").exists()


def test_failed_parquet_write_keeps_previous_file_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "features_202607.parquet"
    store._write(_row_table("original"), path)

    def fail_after_partial_write(table: pa.Table, where: object, **kwargs: object) -> None:
        del table, kwargs
        Path(where).write_bytes(b"partial parquet")
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(store.pq, "write_table", fail_after_partial_write)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        store._write(_row_table("new"), path)

    result = pq.read_table(path)
    assert result.column("address").to_pylist() == ["original"]
    assert not path.with_name(f"{path.name}.lock").exists()
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []
