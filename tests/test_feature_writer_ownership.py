"""Native feature writes must retain ownership for the full transaction."""
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import errno
from pathlib import Path
import os
import threading
import time
import subprocess
import sys
from types import ModuleType

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from features import store
from utils import file_ownership


def test_file_guard_cold_import_does_not_load_market_or_ml_dependencies():
    root = Path(__file__).resolve().parents[1]
    code = (f"import sys; sys.path.insert(0, {str(root)!r}); import utils.file_ownership; "
        "assert not {'pandas', 'numpy', 'lightgbm', 'config.config'} & sys.modules.keys()")
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_declared_utils_module_remains_lazy_and_cached(monkeypatch):
    import utils
    calls = []
    module = ModuleType("utils.logger")
    monkeypatch.delattr(utils, "logger", raising=False)
    monkeypatch.setattr(utils, "import_module", lambda name: calls.append(name) or module)
    assert "logger" in dir(utils)
    assert utils.logger is module and utils.logger is module
    assert calls == ["utils.logger"]
    assert utils.__all__ == ["lista_pares", "descubridor_pares", "logger"]


def test_unknown_utils_attribute_does_not_import_anything(monkeypatch):
    import utils
    monkeypatch.setattr(utils, "import_module", lambda name: pytest.fail("unknown attribute cannot import"))
    with pytest.raises(AttributeError):
        getattr(utils, "not_a_declared_utility")


def table(address):
    return pa.Table.from_pydict({"address": [address], "label": [1],
        "target_total_pnl_pct": [1.], "sample_type": ["shadow_close"]})


def test_old_mtime_cannot_steal_a_live_native_writer(tmp_path, monkeypatch):
    path = tmp_path / "features_202608.parquet"
    store._write(table("original"), path)
    monkeypatch.setattr(store, "_LOCK_TIMEOUT_SECONDS", .05)
    monkeypatch.setattr(store, "_LOCK_STALE_SECONDS", .001)
    monkeypatch.setattr(store, "_LOCK_POLL_SECONDS", .005)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store._exclusive_parquet_lock(path):
            marker = path.with_name(path.name + ".lock")
            record = marker.read_bytes()
            os.utime(marker, (1, 1))
            future = pool.submit(store._write, table("competitor"), path)
            with pytest.raises(TimeoutError):
                future.result(timeout=5)
            assert marker.read_bytes() == record
            assert pq.read_table(path).column("address").to_pylist() == ["original"]


def test_expired_marker_does_not_lose_a_concurrent_native_row(tmp_path, monkeypatch):
    path = tmp_path / "features_202608.parquet"
    store._write(table("original"), path)
    owner_ready, owner_release, competitor_ready = threading.Event(), threading.Event(), threading.Event()
    write = store._atomic_write_table

    def delayed_write(value, destination):
        addresses = value.column("address").to_pylist()
        if "owner" in addresses and "competitor" not in addresses:
            owner_ready.set()
            assert owner_release.wait(5)
        if "competitor" in addresses:
            competitor_ready.set()
        return write(value, destination)

    monkeypatch.setattr(store, "_atomic_write_table", delayed_write)
    monkeypatch.setattr(store, "_LOCK_STALE_SECONDS", .001)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(store._write, table("owner"), path)
        assert owner_ready.wait(5)
        marker = path.with_name(path.name + ".lock")
        os.utime(marker, (1, 1))
        competitor = pool.submit(store._write, table("competitor"), path)
        try:
            # The competitor must never publish before the first writer exits.
            assert not competitor_ready.wait(.2)
        finally:
            owner_release.set()
        assert owner.result(timeout=5) and competitor.result(timeout=5)
    assert pq.read_table(path).column("address").to_pylist() == ["original", "owner", "competitor"]


@pytest.mark.parametrize("live", [False, True])
def test_legacy_owner_pid_not_age_controls_migration(tmp_path, monkeypatch, live):
    path = tmp_path / "features_202608.parquet"
    store._write(table("original"), path)
    marker = path.with_name(path.name + ".lock")
    raw = b"pid=12345 created_at=1\n"
    marker.write_bytes(raw)
    os.utime(marker, (1, 1))
    monkeypatch.setattr(store, "process_is_running", lambda pid: live)
    monkeypatch.setattr(store, "_LOCK_TIMEOUT_SECONDS", 0)
    if live:
        with pytest.raises(TimeoutError):
            store._write(table("new"), path)
        assert marker.read_bytes() == raw
        assert pq.read_table(path).column("address").to_pylist() == ["original"]
    else:
        assert store._write(table("new"), path)
        assert not marker.exists()
        assert pq.read_table(path).column("address").to_pylist() == ["original", "new"]


@pytest.mark.parametrize("raw", [b"other owner", b"", b"pid=0 created_at=1",
    b"pid=-1 created_at=1", b"pid=123 created_at=nan", b"pid=123 created_at=inf",
    b"pid=123 created_at=1 pid=456", b"pid=123 created_at=1 unexpected=field",
    b"pid=123 created_at=1 version=unknown owner_id=" + b"a" * 32,
    b"pid=123 created_at=1 version=os_owned_parquet_writer_v1 owner_id=wrong",
    b"x" * 513, b"\xff"])
def test_unproved_marker_is_preserved_even_when_ancient(tmp_path, monkeypatch, raw):
    path = tmp_path / "features_202608.parquet"
    store._write(table("original"), path)
    marker = path.with_name(path.name + ".lock")
    marker.write_bytes(raw)
    os.utime(marker, (1, 1))
    monkeypatch.setattr(store, "_LOCK_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(store, "process_is_running", lambda pid: pytest.fail("unproved record must not probe PID"))
    with pytest.raises(TimeoutError):
        store._write(table("new"), path)
    assert marker.read_bytes() == raw
    assert pq.read_table(path).column("address").to_pylist() == ["original"]


def test_foreign_inplace_marker_is_not_deleted_on_release(tmp_path):
    path = tmp_path / "features_202608.parquet"
    marker = path.with_name(path.name + ".lock")
    with store._exclusive_parquet_lock(path):
        marker.write_bytes(b"foreign diagnostic")
    assert marker.read_bytes() == b"foreign diagnostic"


@pytest.mark.parametrize("error", [ValueError, KeyboardInterrupt])
def test_original_body_error_releases_owned_guard(tmp_path, error):
    path = tmp_path / "features_202608.parquet"
    marker = path.with_name(path.name + ".lock")
    with pytest.raises(error, match="original body failure"):
        with store._exclusive_parquet_lock(path):
            raise error("original body failure")
    assert not marker.exists()
    assert store._write(table("later"), path)


def test_partial_owner_record_failure_releases_fd_and_guard(tmp_path, monkeypatch):
    path = tmp_path / "features_202608.parquet"
    original_write = store.os.write
    descriptors = []
    with monkeypatch.context() as patch:
        def partial(fd, data):
            descriptors.append(fd)
            return original_write(fd, data[:3])
        patch.setattr(store.os, "write", partial)
        with pytest.raises(OSError, match="Incomplete parquet owner record"):
            store._write(table("failed"), path)
    assert not path.exists() and not path.with_name(path.name + ".lock").exists()
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert store._write(table("later"), path)


def test_stable_guard_inode_survives_multiple_transactions(tmp_path):
    path = tmp_path / "features_202608.parquet"
    guard = path.with_name("." + path.name + ".lock.guard")
    assert store._write(table("first"), path)
    identity = (guard.stat().st_dev, guard.stat().st_ino)
    assert store._write(table("second"), path)
    assert (guard.stat().st_dev, guard.stat().st_ino) == identity
    assert not path.with_name(path.name + ".lock").exists()


def test_replaced_marker_after_fd_close_is_not_deleted(tmp_path, monkeypatch):
    path = tmp_path / "features_202608.parquet"
    marker = path.with_name(path.name + ".lock")
    close = store.os.close
    def replace_marker(fd):
        close(fd)
        marker.unlink()
        marker.write_bytes(b"different owner")
    monkeypatch.setattr(store.os, "close", replace_marker)
    with store._exclusive_parquet_lock(path):
        pass
    assert marker.read_bytes() == b"different owner"


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_guard_acquisition_error_keeps_original_error_and_closes_handle(tmp_path, monkeypatch, error):
    guard = tmp_path / ".guard"
    handles = []
    with monkeypatch.context() as patch:
        def fail(handle):
            handles.append(handle)
            if error is OSError:
                raise OSError(errno.EIO, "original OS acquisition error")
            raise error("original OS acquisition error")
        patch.setattr(file_ownership, "_lock", fail)
        with pytest.raises(error, match="original OS acquisition error"):
            with file_ownership.exclusive_file_guard(guard, deadline=time.monotonic() + 1, poll_seconds=.01):
                pytest.fail("failed acquisition cannot enter")
    assert len(handles) == 1 and handles[0].closed
    with file_ownership.exclusive_file_guard(guard, deadline=time.monotonic() + 1, poll_seconds=.01):
        pass


def test_guard_unlock_error_still_closes_os_owner(tmp_path, monkeypatch):
    guard = tmp_path / ".guard"
    handles = []
    with monkeypatch.context() as patch:
        def fail(handle):
            handles.append(handle)
            raise OSError(errno.EIO, "original OS release error")
        patch.setattr(file_ownership, "_unlock", fail)
        with pytest.raises(OSError, match="original OS release error"):
            with file_ownership.exclusive_file_guard(guard, deadline=time.monotonic() + 1, poll_seconds=.01):
                pass
    assert handles[0].closed
    with file_ownership.exclusive_file_guard(guard, deadline=time.monotonic() + 1, poll_seconds=.01):
        pass


@pytest.mark.parametrize("field,value", [("deadline", float("nan")), ("deadline", float("inf")),
    ("deadline", True), ("deadline", 10**1000), ("poll_seconds", 0),
    ("poll_seconds", -1), ("poll_seconds", float("nan")), ("poll_seconds", True)])
def test_invalid_guard_timing_is_rejected_before_file_creation(tmp_path, field, value):
    guard = tmp_path / ".guard"
    values = {"deadline": time.monotonic() + 1, "poll_seconds": .01, field: value}
    with pytest.raises(ValueError, match="Invalid file ownership"):
        with file_ownership.exclusive_file_guard(guard, **values):
            pytest.fail("invalid timing cannot enter")
    assert not guard.exists()


def _child_owner(path, connection, legacy):
    path = Path(path)
    if legacy:
        path.with_name(path.name + ".lock").write_text(
            f"pid={os.getpid()} created_at={time.time()}\n", encoding="ascii")
        connection.send(True)
        if connection.recv() == "crash":
            os._exit(0)
    else:
        with store._exclusive_parquet_lock(path):
            connection.send(True)
            if connection.recv() == "crash":
                os._exit(0)


@pytest.mark.parametrize("legacy", [False, True])
def test_native_process_owner_and_crash_recovery(tmp_path, monkeypatch, legacy):
    path = tmp_path / "features_202608.parquet"
    store._write(table("original"), path)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_child_owner, args=(str(path), child, legacy))
    process.start()
    child.close()
    try:
        assert parent.poll(15) and parent.recv() is True
        assert file_ownership.process_is_running(process.pid)
        marker = path.with_name(path.name + ".lock")
        original_marker = marker.read_bytes()
        os.utime(marker, (1, 1))
        with monkeypatch.context() as patch:
            patch.setattr(store, "_LOCK_TIMEOUT_SECONDS", .03)
            patch.setattr(store, "_LOCK_POLL_SECONDS", .005)
            with pytest.raises(TimeoutError):
                store._write(table("forbidden"), path)
        assert marker.read_bytes() == original_marker
        parent.send("crash")
        process.join(15)
        assert process.exitcode == 0 and not file_ownership.process_is_running(process.pid)
        assert marker.exists(), "native crash leaves diagnostic evidence, not OS ownership"
        assert store._write(table("recovered"), path)
        assert not marker.exists()
        assert pq.read_table(path).column("address").to_pylist() == ["original", "recovered"]
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
