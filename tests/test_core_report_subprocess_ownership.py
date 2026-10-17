"""Native report child ownership under controlled completion/cancellation."""
import ast
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest


def native_report(monkeypatch, tmp_path, *, code=0):
    started, release = asyncio.Event(), asyncio.Event()
    class Process:
        returncode = None
        async def communicate(self):
            started.set()
            await release.wait()
            self.returncode = code
            return b'{}', b'synthetic report error' if code else b''
        async def wait(self):
            await release.wait()
            self.returncode = code
            return code
    process = Process()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    source = Path(os.environ.get('MEMEBOT_CORE_SOURCE_ROOT', str(Path(__file__).resolve().parents[1])))
    node = next(node for node in ast.parse((source/'run_bot.py').read_text(encoding='utf-8')).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == '_run_core_reports_regeneration_isolated')
    ns = {'asyncio':asyncio, 'sys':sys, 'subprocess':subprocess, 'os':os,
        'PROJECT_ROOT':tmp_path, '_read_json_file':lambda path:{'warnings':{}}}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'run_bot.py', 'exec'), ns)
    return ns['_run_core_reports_regeneration_isolated'], started, release, process, spawn


@pytest.mark.asyncio
@pytest.mark.parametrize('repeated', [False, True])
async def test_native_report_cancellation_retains_child_until_exit(monkeypatch, tmp_path, repeated):
    run, started, release, process, spawn = native_report(monkeypatch, tmp_path)
    task = asyncio.create_task(run())
    try:
        await started.wait()
        task.cancel()
        for _ in range(4): await asyncio.sleep(0)
        if repeated:
            task.cancel()
            for _ in range(4): await asyncio.sleep(0)
        assert not task.done(), 'Original report owner returned while its child was still live'
        assert process.returncode is None
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert process.returncode == 0
        assert spawn.await_count == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [0, 1])
async def test_native_report_completion_retains_original_result_and_failure(monkeypatch, tmp_path, code):
    run, started, release, process, spawn = native_report(monkeypatch, tmp_path, code=code)
    task = asyncio.create_task(run())
    await started.wait()
    release.set()
    if code:
        with pytest.raises(RuntimeError, match='synthetic report error'): await task
    else:
        assert await task == {'warnings':{}}
    assert process.returncode == code and spawn.await_count == 1


@pytest.mark.asyncio
async def test_native_report_pipe_failure_still_drains_child(monkeypatch, tmp_path):
    run, started, release, process, spawn = native_report(monkeypatch, tmp_path)
    error = OSError('synthetic pipe failure')
    async def failed_pipe():
        started.set()
        raise error
    process.communicate = failed_pipe
    task = asyncio.create_task(run())
    try:
        await started.wait()
        for _ in range(4): await asyncio.sleep(0)
        assert not task.done() and process.returncode is None
        release.set()
        with pytest.raises(OSError) as caught: await task
        assert caught.value is error and process.returncode == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_report_cancellation_during_spawn_keeps_exact_admitted_child(monkeypatch, tmp_path):
    run, started, release, process, spawn = native_report(monkeypatch, tmp_path)
    spawning, allow_spawn = asyncio.Event(), asyncio.Event()
    async def delayed_spawn(*args, **kwargs):
        spawning.set()
        await allow_spawn.wait()
        return process
    spawn.side_effect = delayed_spawn
    task = asyncio.create_task(run())
    try:
        await spawning.wait()
        task.cancel()
        for _ in range(4): await asyncio.sleep(0)
        assert not task.done()
        allow_spawn.set()
        await started.wait()
        assert not task.done() and process.returncode is None
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert process.returncode == 0 and spawn.await_count == 1
    finally:
        allow_spawn.set()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
