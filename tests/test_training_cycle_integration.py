"""Native public entrypoints and async ownership, no operational model fits."""
from __future__ import annotations

import ast
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from ml import training_lock as locks


@contextmanager
def foreign_owner(path):
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            assert locks.acquire_lock(path)
            ready.set()
            assert release.wait(5)
        except BaseException as exc:
            errors.append(exc)
        finally:
            locks.release_lock(path)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(2)
        yield
    finally:
        release.set()
        thread.join(3)
        assert not thread.is_alive()
        assert not errors, errors


def test_public_primary_fit_respects_foreign_cycle_owner(tmp_path, monkeypatch):
    from ml import train
    metrics = tmp_path / "metrics"
    monkeypatch.setattr(train, "METRICS_DIR", metrics)
    monkeypatch.setattr(train, "TRAIN_STATUS_JSON", metrics / "train_status.json")
    monkeypatch.setattr(train, "_load_dataset", lambda: pytest.fail("foreign-owned primary must not load/fit"))
    with foreign_owner(metrics / "training.lock"):
        result = train.train_and_save()
    assert result.status == "locked" and result.trained is False
    assert not (metrics / "train_status.json").exists()


def test_public_advisory_fit_respects_foreign_cycle_owner(tmp_path, monkeypatch):
    from ml import runner_advisory_learning as learning
    monkeypatch.setattr(learning, "CFG", SimpleNamespace(ML_RUNNER_ADVISORY_ENABLED=True))
    monkeypatch.setattr(learning, "load_training_frame", lambda *args: pytest.fail("foreign-owned advisory must not load/fit"))
    with foreign_owner(tmp_path / "data/metrics/training.lock"):
        result = learning.train_runner_advisory(root=tmp_path, frame=object())
    assert result["status"] == "locked" and result["updated"] is False
    assert not (tmp_path / "ml/models/runner/advisory_manifest.json").exists()


def test_nested_session_does_not_release_the_original_cycle(tmp_path):
    path = tmp_path / "training.lock"
    with locks.training_session(path) as outer:
        assert outer
        original = path.read_bytes()
        with locks.training_session(path) as inner:
            assert inner
        assert path.read_bytes() == original
        assert locks.acquire_lock(path) is False
    assert not path.exists()


@pytest.mark.parametrize("error", [False, True])
def test_public_primary_entry_releases_only_its_own_claim(tmp_path, monkeypatch, error):
    from ml import train
    monkeypatch.setattr(train, "TRAIN_STATUS_JSON", tmp_path / "train_status.json")
    sentinel = object()

    def fit():
        assert locks.acquire_lock(tmp_path / "training.lock") is False
        if error:
            raise ValueError("isolated primary failure")
        return sentinel

    monkeypatch.setattr(train, "_train_and_save_unlocked", fit)
    if error:
        with pytest.raises(ValueError, match="isolated primary failure"):
            train.train_and_save()
    else:
        assert train.train_and_save() is sentinel
    assert locks.acquire_lock(tmp_path / "training.lock")
    locks.release_lock(tmp_path / "training.lock")


@pytest.mark.parametrize("primary", [False, True])
@pytest.mark.parametrize("advisory", [False, True])
def test_native_cycle_keeps_primary_and_advisory_channels_distinct(tmp_path, monkeypatch, primary, advisory):
    from ml import training_daemon as daemon
    path = tmp_path / "training.lock"
    monkeypatch.setattr(daemon, "STATUS_PATH", tmp_path / "train_status.json")
    monkeypatch.setattr(daemon, "acquire_lock", lambda **kwargs: locks.acquire_lock(path, **kwargs))
    monkeypatch.setattr(daemon, "release_lock", lambda: locks.release_lock(path))
    monkeypatch.setattr(daemon, "feature_dataset_snapshot", lambda: {"usable": True})
    seen = []

    def primary_fit():
        with locks.training_session(path) as nested:
            assert nested
            seen.append("primary")
        return primary

    def advisory_fit():
        assert locks.acquire_lock(path) is False
        seen.append("advisory")
        return {"status": "completed", "updated": advisory}

    monkeypatch.setattr(daemon, "retrain_if_better", primary_fit)
    monkeypatch.setattr(daemon, "train_runner_advisory", advisory_fit)
    result = daemon.run_training_cycle()
    assert result.primary_promoted is primary and result.advisory_updated is advisory
    assert not result.errors and seen == ["primary", "advisory"]
    assert not path.exists()
    status = json.loads((tmp_path / "train_status.json").read_text())
    assert status["updated"] is primary and status["advisory_updated"] is advisory


def runtime_function(cycle, raw):
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_run_retrain_once")
    calls = []

    async def reload():
        calls.append("reload")
        return {"model_loaded": True}

    def scorecard(**kwargs):
        calls.append("scorecard")
        return {"updated": True}

    namespace = {"asyncio": asyncio, "_retrain_lock": asyncio.Lock(), "_runtime_retrain_state": "idle",
        "retrain_if_better": raw, "run_training_cycle": cycle, "_reload_model_now": reload,
        "research_runtime": SimpleNamespace(refresh_scorecard=scorecard),
        "log": SimpleNamespace(info=lambda *args: None), "_note_runtime_error": lambda *args: None}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "run_bot.py", "exec"), namespace)
    return namespace, calls


@pytest.mark.asyncio
async def test_runtime_advisory_update_does_not_claim_primary_promotion():
    cycle = lambda: SimpleNamespace(primary_promoted=False, advisory_updated=True,
                                    status="completed", errors={})
    namespace, calls = runtime_function(cycle, lambda: False)
    result = await namespace["_run_retrain_once"](source="isolated")
    assert result["trained"] is False and result["advisory_updated"] is True
    assert result["reload"] is None and not calls


@pytest.mark.asyncio
async def test_runtime_cancellation_keeps_owner_until_native_worker_drains():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work():
        started.set()
        try:
            assert release.wait(5)
            return SimpleNamespace(primary_promoted=False, advisory_updated=False,
                                   status="not_promoted", errors={})
        finally:
            finished.set()

    namespace, _ = runtime_function(work, work)
    task = asyncio.create_task(namespace["_run_retrain_once"](source="isolated"))
    try:
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(.001)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done(), "an owned native worker cannot outlive its reported cancellation"
        assert namespace["_retrain_lock"].locked()
        assert namespace["_runtime_retrain_state"] == "running"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert finished.wait(2)
    assert namespace["_runtime_retrain_state"] == "idle"
    assert not namespace["_retrain_lock"].locked()


@pytest.mark.asyncio
async def test_runtime_primary_promotion_alone_triggers_reload_and_scorecard():
    cycle = lambda: SimpleNamespace(primary_promoted=True, advisory_updated=False, status="trained", errors={})
    namespace, calls = runtime_function(cycle, lambda: pytest.fail("old raw primary route must not run"))
    result = await namespace["_run_retrain_once"](source="isolated", force_requested=True)
    assert result["trained"] is True and result["advisory_updated"] is False
    assert result["force_requested"] is True and calls == ["reload", "scorecard"]
    assert namespace["_runtime_retrain_state"] == "idle"


@pytest.mark.asyncio
async def test_runtime_partial_cycle_errors_are_not_reported_as_idle_success():
    cycle = lambda: SimpleNamespace(primary_promoted=False, advisory_updated=True,
        status="failed", errors={"entry_training": "ValueError"})
    namespace, calls = runtime_function(cycle, lambda: False)
    result = await namespace["_run_retrain_once"](source="isolated")
    assert result["training_errors"] == {"entry_training": "ValueError"}
    assert result["training_status"] == "failed" and not calls
    assert namespace["_runtime_retrain_state"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_error", [False, True])
async def test_repeated_cancellation_does_not_detach_worker(worker_error):
    from runtime.owned_thread import await_owned_thread
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work():
        started.set()
        try:
            assert release.wait(5)
            if worker_error:
                raise ValueError("isolated late worker failure")
            return "finished"
        finally:
            finished.set()

    task = asyncio.create_task(await_owned_thread(work))
    try:
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(.001)
        assert started.is_set()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()
