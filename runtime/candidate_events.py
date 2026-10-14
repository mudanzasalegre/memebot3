"""Bounded owned candidate diagnostics; never orders or financial close export."""
from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import math
import threading


class CandidateEventWriter:
    def __init__(self, writer, *, capacity=256, max_row_bytes=262144, on_result=None):
        if type(capacity) is not int or not 1 <= capacity <= 4096:
            raise ValueError('Invalid candidate event capacity')
        if type(max_row_bytes) is not int or not 1024 <= max_row_bytes <= 1048576:
            raise ValueError('Invalid candidate event byte budget')
        self.writer, self.on_result = writer, on_result
        self.capacity, self.max_row_bytes = capacity, max_row_bytes
        self._queue = deque()
        self._loop = self._thread = self._wake = self._task = self._pool = None
        self._accepting = self._closing = self._current = False
        self.admitted = self.persisted = self.failed = self.dropped = self.callback_errors = 0
        self.last_drop_reason = 'not_started'

    def start(self):
        if self._task is not None:
            raise RuntimeError('Candidate event writer cannot be restarted')
        self._loop, self._thread = asyncio.get_running_loop(), threading.current_thread()
        self._wake = asyncio.Event()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='memebot-candidate-writer')
        try:
            self._task = self._loop.create_task(self._run(), name='candidate-event-writer')
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

    def _detach(self, value, depth=0, budget=None):
        if budget is None: budget = [self.max_row_bytes]
        budget[0] -= 32
        if budget[0] < 0: raise ValueError('Candidate snapshot exceeds estimated budget')
        if depth > 16:
            raise ValueError('Candidate snapshot nesting exceeds limit')
        if value is None or isinstance(value, bool): return value
        if isinstance(value, int):
            if value.bit_length() > 4096: raise ValueError('Candidate integer exceeds limit')
            return value
        if isinstance(value, str):
            if len(value) > self.max_row_bytes: raise ValueError('Candidate string exceeds limit')
            budget[0] -= len(value.encode('utf-8'))
            if budget[0] < 0: raise ValueError('Candidate snapshot exceeds estimated budget')
            return value
        if isinstance(value, float): return value if math.isfinite(value) else None
        if isinstance(value, dt.datetime): return value.isoformat() if str(value) != 'NaT' else None
        if isinstance(value, dict) and len(value) <= 512:
            return {self._detach(str(key), depth + 1, budget): self._detach(item, depth + 1, budget)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)) and len(value) <= 256:
            return [self._detach(item, depth + 1, budget) for item in value]
        import numpy as np
        import pandas as pd
        if value is pd.NA or value is pd.NaT: return None
        if isinstance(value, np.generic): return self._detach(value.item(), depth + 1, budget)
        raise ValueError('Unsupported candidate snapshot value')

    def submit(self, prepared, *, context=None):
        if not self._owner_context(): return False
        reason = None
        if not self._accepting or self._task.done(): reason = 'closed'
        elif len(self._queue) + int(self._current) >= self.capacity: reason = 'capacity'
        if reason is None:
            try:
                row = prepared['row']
                if (row.get('event_type') not in {'candidate_stage', 'candidate_decision'}
                        or type(prepared['ledger']) is not bool
                        or prepared['ledger'] and row['event_type'] != 'candidate_decision'
                        or any(row.get(key) is not None for key in ('pnl_pct', 'label', 'outcome_trade_id',
                            'outcome_execution_proof', 'outcome_return_basis', 'outcome_observation_id'))):
                    raise ValueError('Outcome/financial record cannot enter candidate queue')
                frozen = self._detach({'prepared': prepared, 'context': context})
                encoded = json.dumps(frozen, separators=(',', ':'), allow_nan=False).encode('utf-8')
                if len(encoded) > self.max_row_bytes: raise ValueError('Candidate snapshot exceeds byte budget')
            except Exception:
                reason = 'invalid_snapshot'
        if reason is not None:
            self.dropped += 1
            self.last_drop_reason = reason
            return False
        self._queue.append(encoded)
        self.admitted += 1
        self._wake.set()
        return True

    def _result(self, context, written, error):
        if self.on_result is not None:
            try: self.on_result(context, written, error)
            except Exception: self.callback_errors += 1

    async def _run(self):
        cancelled = False
        try:
            while self._queue or not self._closing:
                if not self._queue:
                    self._wake.clear()
                    try: await self._wake.wait()
                    except asyncio.CancelledError:
                        cancelled = True
                        self._closing, self._accepting = True, False
                    continue
                job = json.loads(self._queue.popleft())
                self._current = True
                error = None
                try:
                    future = self._loop.run_in_executor(self._pool, self.writer, job['prepared'])
                except Exception as exc:
                    self.failed += 1
                    self._current = False
                    self._result(job['context'], False, exc)
                    continue
                while True:
                    try:
                        result = await asyncio.shield(future)
                        if result is not True: raise RuntimeError('Candidate persistence is unconfirmed')
                        self.persisted += 1
                        break
                    except asyncio.CancelledError:
                        if future.cancelled(): raise RuntimeError('Owned candidate future was cancelled')
                        cancelled = True
                        self._closing, self._accepting = True, False
                    except Exception as exc:
                        self.failed += 1
                        error = exc
                        break
                    except BaseException as exc:
                        self.failed += 1
                        self._current = False
                        self._result(job['context'], False, exc)
                        raise
                self._current = False
                self._result(job['context'], error is None, error)
        finally:
            self._accepting = False
            while self._queue:
                job = json.loads(self._queue.popleft())
                self.failed += 1
                self._result(job['context'], False, RuntimeError('Candidate writer stopped before persistence'))
            self._pool.shutdown(wait=True)
            self._pool = None
            self._current = False
        if cancelled: raise asyncio.CancelledError

    async def close(self):
        if self._task is None: return
        if not self._owner_context() or asyncio.current_task() is self._task:
            raise RuntimeError('Candidate close requires an external owner-loop caller')
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
        return {'admitted': self.admitted, 'persisted': self.persisted, 'failed': self.failed,
            'dropped': self.dropped, 'pending': len(self._queue) + int(self._current),
            'accepting': self._accepting, 'closed': self._task is not None and self._task.done() and self._pool is None,
            'callback_errors': self.callback_errors, 'last_drop_reason': self.last_drop_reason}
