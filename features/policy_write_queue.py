"""Bounded, owned best-effort policy samples; never financial close export."""
from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import datetime as dt
import threading

import numpy as np

from features import store


class PolicyWriteQueue:
    def __init__(self, writer=None, *, capacity=128, max_row_bytes=65536, on_result=None):
        if type(capacity) is not int or not 1 <= capacity <= 8192:
            raise ValueError("Invalid policy write capacity")
        if type(max_row_bytes) is not int or not 1024 <= max_row_bytes <= 1048576:
            raise ValueError("Invalid policy row byte budget")
        self.writer = store.append if writer is None else writer
        self.capacity, self.max_row_bytes = capacity, max_row_bytes
        self.on_result = on_result
        self._queue = deque()
        self._task = self._pool = self._loop = self._wake = None
        self._thread = None
        self._accepting = self._closing = self._current = False
        self.admitted = self.persisted = self.failed = self.dropped = self.callback_errors = 0
        self.last_drop_reason = "not_started"

    def start(self):
        if self._task is not None:
            raise RuntimeError("Policy writer cannot be restarted")
        self._loop = asyncio.get_running_loop()
        self._thread = threading.current_thread()
        self._wake = asyncio.Event()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memebot-policy-writer")
        try:
            self._task = self._loop.create_task(self._run(), name="policy-feature-writer")
        except BaseException:
            self._pool.shutdown(wait=True)
            self._pool = None
            raise
        self._accepting = True

    def _owner_context(self):
        try:
            return threading.current_thread() is self._thread and asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _size(self, value, depth=0):
        if depth > 8:
            raise ValueError("Policy value nesting exceeds budget")
        if value is None or isinstance(value, (bool, float, dt.datetime, dt.date)):
            return 32
        if isinstance(value, int):
            return 32 + (value.bit_length() + 7) // 8
        if isinstance(value, str):
            if len(value) > self.max_row_bytes:
                raise ValueError("Policy string exceeds byte budget")
            return len(value.encode("utf-8"))
        if isinstance(value, bytes):
            return len(value)
        if isinstance(value, np.generic):
            return self._size(value.item(), depth + 1)
        if isinstance(value, dict):
            if len(value) > 256:
                raise ValueError("Policy mapping exceeds budget")
            size = 64 + 16 * len(value)
            for key, item in value.items():
                size += self._size(key, depth + 1) + self._size(item, depth + 1)
                if size > self.max_row_bytes:
                    break
            return size
        if isinstance(value, (list, tuple)) and len(value) <= 256:
            size = 64 + 8 * len(value)
            for item in value:
                size += self._size(item, depth + 1)
                if size > self.max_row_bytes:
                    break
            return size
        raise ValueError("Unsupported policy value")

    def submit(self, vector, *, context=None):
        if not self._owner_context():
            return False  # Foreign callers cannot mutate owner counters/queue.
        reason = None
        if not self._accepting or self._task.done():
            reason = "closed"
        elif len(self._queue) + int(self._current) >= self.capacity:
            reason = "capacity"
        if reason is None:
            try:
                if any(store._normalize_scalar(vector.get(key)) is not None for key in
                       ("outcome_trade_id", "outcome_execution_proof", "outcome_return_basis")):
                    raise ValueError("Financial close cannot enter policy queue")
                proofs = (store.PROOF_COLUMN, store.CLOCK_PROOF_COLUMN, store.VALUATION_PROOF_COLUMN)
                attrs = getattr(vector, "attrs", {}) or {}
                row = {key: store._normalize_scalar(vector.get(key)) for key in [*store._FEAT_COLS, *proofs]}
                for key in proofs:
                    if attrs.get(key) is not None:
                        row[key] = attrs[key]
                if self._size(row) + self._size(context) > self.max_row_bytes:
                    raise ValueError("Policy row exceeds byte budget")
                row = deepcopy(row)
                context = deepcopy(context)
            except Exception:
                reason = "invalid_snapshot"
        if reason is not None:
            self.dropped += 1
            self.last_drop_reason = reason
            return False
        # Partition uses original admission time; timestamp remains the original
        # feature T0 and ts remains the actual persistence time in store.append.
        self._queue.append((row, context, dt.datetime.now(dt.timezone.utc)))
        self.admitted += 1
        self._wake.set()
        return True

    def _result(self, context, written, error):
        if self.on_result is not None:
            try:
                self.on_result(context, written, error)
            except Exception:
                self.callback_errors += 1

    async def _run(self):
        cancelled = False
        try:
            while self._queue or not self._closing:
                if not self._queue:
                    self._wake.clear()
                    try:
                        await self._wake.wait()
                    except asyncio.CancelledError:
                        cancelled = True
                        self._closing, self._accepting = True, False
                    continue
                row, context, partition = self._queue.popleft()
                self._current = True
                try:
                    future = self._loop.run_in_executor(self._pool, lambda: self.writer(
                        row, 0, sample_type="policy_reject", strict=True, partition_at=partition))
                except Exception as exc:
                    self.failed += 1
                    self._current = False
                    self._result(context, False, exc)
                    continue
                error = None
                while True:
                    try:
                        written = await asyncio.shield(future)
                        if written is not True:
                            raise RuntimeError("Policy writer did not confirm persistence")
                        self.persisted += 1
                        break
                    except asyncio.CancelledError:
                        if future.cancelled():
                            raise RuntimeError("Owned policy write future was cancelled")
                        cancelled = True
                        self._closing, self._accepting = True, False
                    except Exception as exc:
                        self.failed += 1
                        error = exc
                        break
                self._current = False
                self._result(context, error is None, error)
        finally:
            self._accepting = False
            while self._queue:
                _, context, _ = self._queue.popleft()
                self.failed += 1
                self._result(context, False, RuntimeError("Policy writer stopped before persistence"))
            self._pool.shutdown(wait=True)
            self._pool = None
            self._current = False
        if cancelled:
            raise asyncio.CancelledError

    async def close(self):
        if self._task is None:
            return
        if not self._owner_context() or asyncio.current_task() is self._task:
            raise RuntimeError("Policy writer close requires an external owner-loop caller")
        self._accepting, self._closing = False, True
        self._wake.set()
        cancellation = None
        while not self._task.done():
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError as exc:
                if asyncio.current_task().cancelling():
                    cancellation = exc
        if not self._task.cancelled():
            self._task.result()
        if cancellation is not None:
            raise cancellation

    def snapshot(self):
        return {"admitted": self.admitted, "persisted": self.persisted, "failed": self.failed,
            "dropped": self.dropped, "pending": len(self._queue) + int(self._current),
            "accepting": self._accepting, "closed": self._task is not None and self._task.done() and self._pool is None,
            "last_drop_reason": self.last_drop_reason, "callback_errors": self.callback_errors}
