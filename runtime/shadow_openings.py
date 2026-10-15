"""Bounded owner-loop shadow work, never orders or model activation.

Admission is memory-only and not a durable observation. The native handler
owns subsequent price lookup/checkpoint acknowledgement and cancellation IO.
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
import math
import threading
import time

from runtime.shadow_closes import _freeze
from runtime.shadow_state import snapshot_vector

FIELDS = {'address', 'vector', 'price_hint', 'force', 'regime', 'reason', 'stage',
    'proba', 'threshold', 'rank_info', 'shadow_kind', 'opening_snapshot'}


def _bounded(value, budget, depth=0):
    budget[0] -= 32
    if budget[0] < 0 or depth > 18: raise ValueError('Shadow request exceeds structural budget')
    if isinstance(value, str):
        if len(value) > budget[0]: raise ValueError('Oversized shadow string')
        budget[0] -= len(value.encode('utf-8'))
    elif isinstance(value, int) and value.bit_length() > 4096:
        raise ValueError('Oversized shadow integer')
    elif isinstance(value, dict):
        if len(value) > 2048: raise ValueError('Oversized shadow mapping')
        for key, item in value.items():
            _bounded(key, budget, depth + 1)
            _bounded(item, budget, depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > 2048: raise ValueError('Oversized shadow sequence')
        for item in value: _bounded(item, budget, depth + 1)
    if budget[0] < 0: raise ValueError('Shadow request exceeds byte budget')


class ShadowOpeningQueue:
    def __init__(self, handler, *, ready, capacity=64, max_bytes=2097152,
                 timeout_s=120., max_wait_s=120., on_result=None, clock=time.monotonic):
        if type(capacity) is not int or not 1 <= capacity <= 256:
            raise ValueError('Invalid shadow opening capacity')
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 2097152:
            raise ValueError('Invalid shadow opening byte budget')
        if any(type(value) not in (int, float) or not math.isfinite(value)
               or not 0 < value <= 3600 for value in (timeout_s, max_wait_s)):
            raise ValueError('Invalid shadow opening time budget')
        self.handler, self.ready, self.on_result, self.clock = handler, ready, on_result, clock
        self.capacity, self.max_bytes = capacity, max_bytes
        self.timeout_s, self.max_wait_s = timeout_s, max_wait_s
        self._queue, self._pending = deque(), {}
        self._loop = self._thread = self._wake = self._task = self._ready_task = None
        self._current = None
        self._accepting = self._closing = self._closed = False
        self.admitted = self.completed = self.failed = self.dropped = self.expired = self.callback_errors = 0
        self.last_drop_reason = 'not_started'

    def _owner_context(self):
        try:
            return threading.current_thread() is self._thread and asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def start(self):
        if self._task is not None or self._ready_task is not None or self._closed:
            raise RuntimeError('Shadow opening queue cannot restart')
        self._loop, self._thread = asyncio.get_running_loop(), threading.current_thread()
        self._wake = asyncio.Event()
        async def became_ready():
            await self.ready.wait()
            self._wake.set()
        ready_coro = became_ready()
        try:
            self._ready_task = self._loop.create_task(ready_coro, name='shadow-opening-readiness')
        except BaseException:
            ready_coro.close()
            self._closed = True
            raise
        worker_coro = self._run()
        try:
            self._task = self._loop.create_task(worker_coro, name='shadow-opening-owner')
        except BaseException:
            worker_coro.close()
            self._ready_task.cancel()
            raise
        def readiness_done(task):
            if not task.cancelled() and task.exception() is not None and not self._task.done():
                self._task.cancel()
        self._ready_task.add_done_callback(readiness_done)
        self._accepting = True

    def submit(self, request):
        if not self._owner_context(): return False
        reason = None
        if not self._accepting or self._task.done(): reason = 'closed'
        elif len(self._pending) >= self.capacity: reason = 'capacity'
        try:
            if reason is None:
                if (not isinstance(request, dict) or set(request) != FIELDS
                        or not isinstance(request['address'], str) or not request['address'].strip()
                        or len(request['address']) > 4096 or type(request['force']) is not bool
                        or not isinstance(request['vector'], (dict, type(None)))
                        or not isinstance(request['opening_snapshot'], dict)):
                    raise ValueError('Invalid shadow opening request')
                address = request['address']
                if address in self._pending: reason = 'duplicate'
                else:
                    _bounded(request, [self.max_bytes])
                    # Each field has the original snapshot depth budget; do
                    # not impose extra nesting merely for queue transport.
                    frozen = {key: snapshot_vector(value) if key == 'vector' else _freeze(value)
                        for key, value in request.items()}
                    encoded = json.dumps(frozen, separators=(',', ':'), allow_nan=False).encode('utf-8')
                    if len(encoded) > self.max_bytes: raise ValueError('Shadow opening exceeds byte budget')
        except Exception:
            reason = 'invalid_snapshot'
        if reason is not None:
            self.dropped += 1
            self.last_drop_reason = reason
            return False
        self._pending[address] = {'regime': frozen['regime'] or 'dex_mature', 'shadow_kind': frozen['shadow_kind']}
        self._queue.append((address, self.clock(), encoded))
        self.admitted += 1
        self._wake.set()
        return True

    def pending_tokens(self):
        if not self._owner_context(): return {}
        return {address: dict(value) for address, value in self._pending.items()}

    def _result(self, address, success, error):
        self._pending.pop(address, None)
        if self.on_result is not None:
            try: self.on_result(address, success, error)
            except Exception: self.callback_errors += 1

    async def _call(self, request):
        return await asyncio.wait_for(self.handler(request['address'], request['vector'],
            **{key: value for key, value in request.items()
                if key not in {'address', 'vector', 'opening_snapshot'}},
            _opening_snapshot=request['opening_snapshot']), timeout=self.timeout_s)

    async def _drain_readiness(self):
        if self._ready_task is None: return False
        self._ready_task.cancel()
        cancelled = False
        while not self._ready_task.done():
            try: await asyncio.shield(self._ready_task)
            except asyncio.CancelledError:
                if not self._ready_task.cancelled(): cancelled = True
        if not self._ready_task.cancelled(): self._ready_task.result()
        return cancelled

    async def _run(self):
        cancelled = False
        try:
            while self._queue or not self._closing:
                if not self._queue or not self.ready.is_set() and not self._closing:
                    self._wake.clear()
                    if self._queue and self.ready.is_set(): continue
                    try: await self._wake.wait()
                    except asyncio.CancelledError:
                        cancelled = True
                        self._closing, self._accepting = True, False
                    continue
                address, admitted_at, encoded = self._queue.popleft()
                if not self.ready.is_set() or self.clock() - admitted_at > self.max_wait_s:
                    self.failed += 1
                    if self.ready.is_set(): self.expired += 1
                    self._result(address, False, RuntimeError('Shadow opening expired or startup did not complete'))
                    continue
                error = None
                self._current = self._loop.create_task(self._call(json.loads(encoded)), name='owned-shadow-opening')
                while True:
                    try:
                        result = await asyncio.shield(self._current)
                        if result is not True: raise RuntimeError('Shadow opening is unconfirmed')
                        self.completed += 1
                        break
                    except asyncio.CancelledError:
                        if self._current.cancelled():
                            self.failed += 1
                            error = RuntimeError('Shadow opening handler cancelled')
                            break
                        cancelled = True
                        self._closing, self._accepting = True, False
                    except Exception as exc:
                        self.failed += 1
                        error = exc
                        break
                    except BaseException as exc:
                        self.failed += 1
                        self._result(address, False, exc)
                        raise
                self._current = None
                self._result(address, error is None, error)
        finally:
            self._accepting = False
            while self._queue:
                address, _, _ = self._queue.popleft()
                self.failed += 1
                self._result(address, False, RuntimeError('Shadow opening owner stopped'))
            for address in list(self._pending):
                self.failed += 1
                self._result(address, False, RuntimeError('Shadow opening owner failed'))
            try:
                cancelled |= await self._drain_readiness()
            finally:
                self._current = None
                self._closed = True
        if cancelled: raise asyncio.CancelledError

    async def watch(self):
        if not self._owner_context(): raise RuntimeError('Shadow watch requires its owner loop')
        await asyncio.shield(self._task)
        if not self._closing: raise RuntimeError('Shadow opening owner ended unexpectedly')

    async def close(self):
        if self._task is None:
            if self._ready_task is not None:
                if not self._owner_context(): raise RuntimeError('Shadow close requires its owner loop')
                await self._drain_readiness()
            self._closed = True
            return
        if not self._owner_context() or asyncio.current_task() is self._task:
            raise RuntimeError('Shadow close requires an external owner-loop caller')
        self._accepting, self._closing = False, True
        self._wake.set()
        cancellation = None
        while not self._task.done():
            try: await asyncio.shield(self._task)
            except asyncio.CancelledError as exc:
                if asyncio.current_task().cancelling(): cancellation = exc
        if not self._task.cancelled(): self._task.result()
        if cancellation is not None: raise cancellation

    def snapshot(self):
        return {'admitted': self.admitted, 'completed': self.completed, 'failed': self.failed,
            'dropped': self.dropped, 'expired': self.expired, 'pending': len(self._pending),
            'accepting': self._accepting, 'closed': self._closed,
            'callback_errors': self.callback_errors, 'last_drop_reason': self.last_drop_reason}
