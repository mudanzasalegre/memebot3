"""Bounded awaited request ownership and same-loop coalescence, never detached."""
import asyncio
import threading

from utils.bounded_state import bounded_int


class OwnedRequests:
    """Normal callers share latest work; force callers always own a new request.

    Owner cancellation cancels its child once, drains acknowledgement even
    under repeated stop signals, then propagates cancellation. Joiner stop
    never cancels the owner. Cancelled owners release unknown to joiners.
    Loop buckets disappear when idle; all owners and joiners share hard bounds.
    """
    def __init__(self, max_owners=16, max_joiners=256, *, name="provider-request"):
        self.max_owners = bounded_int(max_owners, 16, 1, 128)
        self.max_joiners = bounded_int(max_joiners, 256, 1, 4096)
        self.name = name
        self._loops = {}
        self._lock = threading.RLock()
        self._owners = self._joiners = self._coalesced = self._rejected = 0

    def snapshot(self):
        with self._lock:
            return {"owners": self._owners, "joiners": self._joiners,
                "max_owners": self.max_owners, "max_joiners": self.max_joiners,
                "loop_buckets": len(self._loops), "coalesced": self._coalesced,
                "capacity_rejections": self._rejected,
                "basis": "local_ownership_not_wire_billing_or_entitlement"}

    @staticmethod
    async def _await_owned(factory, name):
        task = asyncio.create_task(factory(), name=name)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
                except Exception: break
            try: task.result()
            except (asyncio.CancelledError, Exception): pass
            raise

    async def run(self, key, factory, *, force_refresh=False):
        loop = asyncio.get_running_loop()
        with self._lock:
            bucket = self._loops.get(loop)
            current = None if force_refresh or bucket is None else bucket["latest"].get(key)
            if current is not None:
                if self._joiners >= self.max_joiners:
                    self._rejected += 1
                    return None
                self._joiners += 1
                self._coalesced += 1
                joiner = True
            else:
                if self._owners >= self.max_owners:
                    self._rejected += 1
                    return None
                if bucket is None:
                    bucket = {"latest": {}, "owners": set()}
                    self._loops[loop] = bucket
                current = loop.create_future()
                current.add_done_callback(lambda f: None if f.cancelled() else f.exception())
                bucket["latest"][key] = current
                bucket["owners"].add(current)
                self._owners += 1
                joiner = False
        if joiner:
            try: return await asyncio.shield(current)
            finally:
                with self._lock: self._joiners -= 1
        try:
            result = await self._await_owned(factory, self.name)
            current.set_result(result)
            return result
        except asyncio.CancelledError:
            if not current.done(): current.set_result(None)
            raise
        except BaseException as error:
            if not current.done(): current.set_exception(error)
            raise
        finally:
            with self._lock:
                self._owners -= 1
                bucket["owners"].discard(current)
                if bucket["latest"].get(key) is current: bucket["latest"].pop(key, None)
                if not bucket["owners"]: self._loops.pop(loop, None)
