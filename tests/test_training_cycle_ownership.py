"""Native training ownership checks; no operational fit or model activation."""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import queue
import threading

import pytest

from ml import training_lock as locks


def _in_thread(action):
    result, errors = [], []

    def run():
        try:
            result.append(action())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "bounded ownership test did not drain"
    if errors:
        raise errors[0]
    return result[0]


def test_live_training_owner_cannot_be_stolen_after_ttl(tmp_path, monkeypatch):
    path = tmp_path / "training.lock"
    assert locks.acquire_lock(path, ttl_s=1)
    original = path.read_bytes()
    now = locks.time.time()
    monkeypatch.setattr(locks.time, "time", lambda: now + 3600)
    try:
        assert _in_thread(lambda: locks.acquire_lock(path, ttl_s=1)) is False
        assert path.read_bytes() == original
    finally:
        locks.release_lock(path)


def test_foreign_thread_cannot_release_training_owner(tmp_path):
    path = tmp_path / "training.lock"
    assert locks.acquire_lock(path)
    original = path.read_bytes()
    try:
        _in_thread(lambda: locks.release_lock(path))
        assert path.is_file()
        assert path.read_bytes() == original
        assert _in_thread(lambda: locks.acquire_lock(path)) is False
    finally:
        locks.release_lock(path)


def _daemon_fixture(tmp_path, monkeypatch):
    from ml import training_daemon as daemon

    path = tmp_path / "train_status.json"
    original = {"status": "trained", "last_train_status": "trained",
                "last_train_attempt_at": "2026-01-01T00:00:00Z",
                "activation_ready": False, "candidate_model_id": "historical-candidate"}
    path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(daemon, "STATUS_PATH", path)
    return daemon, path, original


def test_busy_cycle_preserves_original_training_attempt(tmp_path, monkeypatch):
    daemon, path, original = _daemon_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon, "acquire_lock", lambda **kwargs: False)
    monkeypatch.setattr(daemon, "feature_dataset_snapshot", lambda: {"usable": False})
    monkeypatch.setattr(daemon, "retrain_if_better", lambda: pytest.fail("busy cycle must not fit"))
    assert daemon.train_once() is False
    assert json.loads(path.read_text(encoding="utf-8")) == original
    cycle = json.loads(path.with_name("training_daemon_status.json").read_text(encoding="utf-8"))
    assert cycle["daemon_status"] == "locked"


def test_cycle_snapshot_failure_preserves_original_training_attempt(tmp_path, monkeypatch):
    daemon, path, original = _daemon_fixture(tmp_path, monkeypatch)
    released = []
    monkeypatch.setattr(daemon, "acquire_lock", lambda **kwargs: True)
    monkeypatch.setattr(daemon, "release_lock", lambda: released.append(True))
    monkeypatch.setattr(daemon, "retrain_if_better", lambda: False)
    calls = []

    def snapshot():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("isolated snapshot failure")
        return {"usable": False}

    monkeypatch.setattr(daemon, "feature_dataset_snapshot", snapshot)
    with pytest.raises(OSError, match="isolated snapshot failure"):
        daemon.train_once()
    status = json.loads(path.read_text(encoding="utf-8"))
    assert all(status[key] == value for key, value in original.items())
    assert status["daemon_status"] == "failed"
    assert released == [True]


def test_same_owner_is_not_reentrant_and_release_is_idempotent(tmp_path):
    path = tmp_path / "training.lock"
    assert locks.acquire_lock(path)
    guard = path.with_name("." + path.name + ".guard")
    inode = guard.stat().st_ino
    try:
        assert locks.acquire_lock(path) is False
    finally:
        locks.release_lock(path)
    locks.release_lock(path)
    assert not path.exists()
    assert guard.is_file()
    assert locks.acquire_lock(path)
    assert guard.stat().st_ino == inode
    locks.release_lock(path)


def test_normalized_aliases_share_one_training_owner(tmp_path):
    path = tmp_path / "training.lock"
    alias = tmp_path / "unused" / ".." / "training.lock"
    assert locks.acquire_lock(path)
    try:
        assert _in_thread(lambda: locks.acquire_lock(alias)) is False
    finally:
        locks.release_lock(alias)
    assert not path.exists()


def test_simultaneous_training_requests_have_one_owner(tmp_path):
    path = tmp_path / "training.lock"
    barrier, done = threading.Barrier(8), threading.Event()
    results = queue.Queue()

    def request():
        acquired = False
        try:
            barrier.wait(5)
            acquired = locks.acquire_lock(path)
            results.put(acquired)
            if acquired:
                assert done.wait(5)
        except BaseException as exc:
            results.put(exc)
        finally:
            if acquired:
                locks.release_lock(path)

    workers = [threading.Thread(target=request) for _ in range(8)]
    for worker in workers:
        worker.start()
    try:
        observed = [results.get(timeout=5) for _ in workers]
        assert all(type(value) is bool for value in observed), observed
        assert sum(observed) == 1
    finally:
        done.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
    assert not path.exists()


@pytest.mark.parametrize("ttl", [0, -1, True, 1.5, float("nan"), None])
def test_invalid_ttl_never_creates_ownership(tmp_path, ttl):
    path = tmp_path / "training.lock"
    with pytest.raises(ValueError, match="positive integer"):
        locks.acquire_lock(path, ttl_s=ttl)
    assert not path.exists()


@pytest.mark.parametrize("live", [True, False])
def test_legacy_pid_not_ttl_controls_safe_migration(tmp_path, monkeypatch, live):
    path = tmp_path / "training.lock"
    original = {"pid": 12345, "created_epoch_s": 1}
    path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(locks, "_pid_is_running", lambda pid: live)
    assert locks.acquire_lock(path, ttl_s=1) is not live
    if live:
        assert json.loads(path.read_text(encoding="utf-8")) == original
    else:
        assert json.loads(path.read_text(encoding="utf-8"))["version"] == locks.VERSION
        locks.release_lock(path)


@pytest.mark.parametrize("raw", ["not-json", "[]", '{"pid":true,"created_epoch_s":1}',
                                 '{"version":"unknown"}', '{"pid":12,"created_epoch_s":null}'])
def test_unproved_legacy_owner_is_preserved(tmp_path, raw):
    path = tmp_path / "training.lock"
    path.write_text(raw, encoding="utf-8")
    assert locks.acquire_lock(path) is False
    locks.release_lock(path)
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_owner_publication_failure_releases_os_lock(tmp_path, monkeypatch, failure):
    path = tmp_path / "training.lock"
    with monkeypatch.context() as patch:
        patch.setattr(locks, "write_json_atomic", lambda *args: (_ for _ in ()).throw(failure("disk fault")))
        with pytest.raises(failure, match="disk fault"):
            locks.acquire_lock(path)
    assert locks.acquire_lock(path)
    locks.release_lock(path)


def test_release_does_not_delete_replaced_diagnostic_receipt(tmp_path):
    path = tmp_path / "training.lock"
    assert locks.acquire_lock(path)
    replacement = {"version": locks.VERSION, "owner_id": "different-owner"}
    path.write_text(json.dumps(replacement), encoding="utf-8")
    locks.release_lock(path)
    assert json.loads(path.read_text(encoding="utf-8")) == replacement
    assert locks.acquire_lock(path)
    locks.release_lock(path)


def _process_owner(path, connection):
    acquired = locks.acquire_lock(Path(path), ttl_s=1)
    connection.send(acquired)
    try:
        if connection.recv() == "crash":
            os._exit(0)  # OS must release ownership; diagnostic receipt remains.
    finally:
        if acquired:
            locks.release_lock(Path(path))
        connection.close()


@pytest.mark.parametrize("crash", [False, True])
def test_native_process_owner_and_crash_recovery(tmp_path, monkeypatch, crash):
    path = tmp_path / "training.lock"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_process_owner, args=(str(path), child))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "isolated lock helper did not report ownership"
        assert parent.recv() is True
        original = path.read_bytes()
        monkeypatch.setattr(locks.time, "time", lambda: 2**40)
        assert locks.acquire_lock(path, ttl_s=1) is False
        locks.release_lock(path)
        assert path.read_bytes() == original
        parent.send("crash" if crash else "release")
        process.join(10)
        assert process.exitcode == 0
        assert path.exists() is crash
        assert locks.acquire_lock(path)
        locks.release_lock(path)
        assert not path.exists()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()


def test_current_process_liveness_is_observed_without_signals_on_windows():
    assert locks._pid_is_running(os.getpid()) is True


def test_actual_live_legacy_process_cannot_be_stolen(tmp_path):
    path = tmp_path / "training.lock"
    original = {"pid": os.getpid(), "created_epoch_s": 1}
    path.write_text(json.dumps(original), encoding="utf-8")
    assert locks.acquire_lock(path, ttl_s=1) is False
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_busy_cycle_cannot_overwrite_concurrent_primary_publication(tmp_path, monkeypatch):
    daemon, path, _ = _daemon_fixture(tmp_path, monkeypatch)
    concurrent = {"status": "trained", "candidate_model_id": "new-independent-attempt"}
    monkeypatch.setattr(daemon, "acquire_lock", lambda **kwargs: False)

    def snapshot():
        path.write_text(json.dumps(concurrent), encoding="utf-8")
        return {"usable": True}

    monkeypatch.setattr(daemon, "feature_dataset_snapshot", snapshot)
    assert daemon.train_once() is False
    assert json.loads(path.read_text(encoding="utf-8")) == concurrent
