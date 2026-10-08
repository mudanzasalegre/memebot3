from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import os
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
    addresses = [f"mint-{index}" for index in range(36)]

    for start in range(0, len(addresses), 12):
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda address: store._write(_row_table(address), path), addresses[start:start + 12]))

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


def _windows_permission_error() -> PermissionError:
    error = PermissionError(errno.EACCES, "simulated Windows sharing conflict")
    error.winerror = 32
    return error


def test_transient_windows_lock_open_requires_successful_exclusive_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "features.parquet"
    lock_path = path.with_name(f"{path.name}.lock")
    original_open = store.os.open
    attempts = 0

    def conflicting_open(file: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal attempts
        if Path(file) == lock_path:
            attempts += 1
            assert flags & os.O_EXCL
            if attempts <= 2:
                assert not path.exists()
                raise _windows_permission_error()
        return original_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", conflicting_open)
    store._write(_row_table("new"), path)

    assert attempts == 3
    assert pq.read_table(path).column("address").to_pylist() == ["new"]
    assert not lock_path.exists()


def test_persistent_windows_permission_preserves_data_and_other_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "features.parquet"
    store._write(_row_table("original"), path)
    original_bytes = path.read_bytes()
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.write_bytes(b"other owner")
    original_open = store.os.open
    attempts = 0

    def denied_open(file: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal attempts
        if Path(file) == lock_path:
            attempts += 1
            raise _windows_permission_error()
        return original_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", denied_open)
    monkeypatch.setattr(store, "_LOCK_TIMEOUT_SECONDS", 0)
    with pytest.raises(PermissionError, match="Windows sharing conflict"):
        store._write(_row_table("new"), path)

    assert attempts == 1
    assert path.read_bytes() == original_bytes
    assert lock_path.read_bytes() == b"other owner"


def test_unrecognized_permission_error_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "features.parquet"
    lock_path = path.with_name(f"{path.name}.lock")
    original_open = store.os.open

    def denied_open(file: object, flags: int, *args: object, **kwargs: object) -> int:
        if Path(file) == lock_path:
            raise PermissionError(errno.EPERM, "unrecognized permission failure")
        return original_open(file, flags, *args, **kwargs)

    def no_retry(delay: float) -> None:
        raise AssertionError("unexpected permission retry")

    monkeypatch.setattr(store.os, "open", denied_open)
    monkeypatch.setattr(store.time, "sleep", no_retry)
    with pytest.raises(PermissionError, match="unrecognized permission failure"):
        store._write(_row_table("new"), path)
    assert not path.exists()
    assert not lock_path.exists()


def test_owner_record_failure_releases_fd_without_entering_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "features.parquet"
    store._write(_row_table("original"), path)
    original_bytes = path.read_bytes()
    opened: list[int] = []

    def failed_owner_record(fd: int, data: bytes) -> int:
        opened.append(fd)
        raise PermissionError(errno.EACCES, "owner record denied")

    monkeypatch.setattr(store.os, "write", failed_owner_record)
    with pytest.raises(PermissionError, match="owner record denied"):
        store._write(_row_table("new"), path)

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert path.read_bytes() == original_bytes
    assert not path.with_name(f"{path.name}.lock").exists()


def test_windows_lock_stat_conflict_waits_for_real_owner_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "features.parquet"
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.write_bytes(b"other owner")
    original_stat = Path.stat
    sleeps = 0

    def conflicting_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self == lock_path and sleeps == 0:
            raise _windows_permission_error()
        return original_stat(self, *args, **kwargs)

    def owner_release(delay: float) -> None:
        nonlocal sleeps
        assert path.exists() is False
        assert lock_path.read_bytes() == b"other owner"
        sleeps += 1
        lock_path.unlink()

    monkeypatch.setattr(Path, "stat", conflicting_stat)
    monkeypatch.setattr(store.time, "sleep", owner_release)
    store._write(_row_table("new"), path)

    assert sleeps == 1
    assert pq.read_table(path).column("address").to_pylist() == ["new"]
    assert not lock_path.exists()
