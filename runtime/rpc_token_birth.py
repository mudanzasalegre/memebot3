"""Process-bounded RPC birth ownership, pacing and detached immutable cache."""
import asyncio
from collections import OrderedDict, deque
from copy import deepcopy
import math
import threading
import time

from analytics.api_budget import provider_status
from analytics.mint_creation_reference import FIELD as REFERENCE_FIELD, checked_reference
from analytics.token_birth import FIELD, birth_clock, checked_token_birth, merge_birth_context
from analytics.token_time import BIRTH_CLOCK_FIELDS
from fetcher.mint_initialization import get_mint_initialization
from utils.bounded_state import bounded_int
from utils.request_ownership import OwnedRequests
from utils import solana_execution as rpc


def _bounded(value, default, low, high):
    try:
        parsed = float(value)
        if isinstance(value, bool) or not math.isfinite(parsed): return default
        return max(low, min(high, parsed))
    except (ValueError, TypeError, OverflowError): return default


class _AdmissionDenied(Exception):
    pass


class RpcTokenBirthResolver:
    """Same-loop joiners share work; all loops/threads share hard local bounds.

    Signature, reference and configured node are frozen before admission. A
    negative observation cannot poison a changed reference/node; positive birth
    retains its original receipt independently of later configuration. None of
    this establishes archive-first creation, provider entitlement or buy safety.
    """
    def __init__(self, *, lookup=get_mint_initialization, endpoint=rpc.configured_endpoint,
                 degraded=None, rpm=12, timeout_s=3., cache_size=2048, max_inflight=2,
                 max_joiners=256, negative_ttl_s=5., clock=time.monotonic, wall=time.time):
        self.lookup, self.endpoint = lookup, endpoint
        self.degraded = degraded or (lambda: provider_status('rpc')['degraded'])
        self.rpm = bounded_int(rpm, 12, 1, 60)
        self.timeout_s = _bounded(timeout_s, 3., .05, 8.)
        self.cache_size = bounded_int(cache_size, 2048, 1, 10000)
        self.negative_ttl_s = _bounded(negative_ttl_s, 5., 1., 600.)
        self.clock, self.wall = clock, wall
        self._lock = threading.RLock()
        self._requests = OwnedRequests(bounded_int(max_inflight, 2, 1, 8),
            bounded_int(max_joiners, 256, 1, 4096), name='rpc-mint-birth-lookup')
        self._cache, self._reservations = OrderedDict(), deque()
        self._next_request_at = self._last_clock = None
        self._counts = {}

    def snapshot(self):
        with self._lock:
            state = {'cache_size':len(self._cache), 'lookup_reservations':dict(self._counts),
                'creation_lookup_rpm':self.rpm, 'max_creation_rpc_requests_per_minute':self.rpm,
                'timeout_s':self.timeout_s, 'negative_ttl_s':self.negative_ttl_s,
                'basis':'local_creation_only_upper_bound_not_total_rpc_budget_or_entitlement'}
        state['ownership'] = self._requests.snapshot()
        return state

    def _now_locked(self):
        try:
            now = self.clock()
            if type(now) not in (int, float) or not math.isfinite(now): raise ValueError
            if self._last_clock is not None and now < self._last_clock: raise ValueError
            self._last_clock = now
            return now
        except (ValueError, TypeError, OverflowError):
            raise _AdmissionDenied('clock_unavailable') from None

    def _finish(self, token, receipt, status):
        try: out = merge_birth_context(token, receipt or {}, now=self._wall_now())
        except _AdmissionDenied:
            out, status = deepcopy(token), 'clock_unavailable'
        out['token_birth_enrichment'] = {'status':status, 'source':'solana_rpc',
            'basis':'bounded_creation_lookup_not_buy_permission'}
        with self._lock:
            self._counts[status] = self._counts.get(status, 0) + 1
        return out

    def _wall_now(self):
        try:
            now = self.wall()
            if type(now) not in (int, float) or not math.isfinite(now) or not 0 < now < 1e11:
                raise ValueError
            return now
        except (ValueError, TypeError, OverflowError):
            raise _AdmissionDenied('clock_unavailable') from None

    def _admit(self):
        with self._lock:
            now = self._now_locked()
            while self._reservations and now - self._reservations[0] >= 60.:
                self._reservations.popleft()
            next_at = now + 60. / self.rpm
            if not math.isfinite(next_at) or next_at <= now:
                raise _AdmissionDenied('clock_unavailable')
            if len(self._reservations) >= self.rpm or self._next_request_at is not None and now < self._next_request_at:
                raise _AdmissionDenied('creation_budget')
            self._reservations.append(now)
            self._next_request_at = next_at

    def _cached(self, address, key=None):
        with self._lock:
            now = self._now_locked()
            for name, (expiry, _) in list(self._cache.items()):
                if expiry <= now: self._cache.pop(name, None)
            names = [('positive', address)] + ([('negative', *key)] if key is not None else [])
            for name in names:
                if name in self._cache:
                    self._cache.move_to_end(name)
                    return True, deepcopy(self._cache[name][1])
        return False, None

    def _remember(self, address, key, receipt):
        with self._lock:
            now = self._now_locked()
            original = self._cache.get(('positive', address))
            if original is not None and original[0] > now:
                return deepcopy(original[1])
            if receipt is None:
                name, ttl = ('negative', *key), self.negative_ttl_s
            else:
                name = ('positive', address)
                wall = self._wall_now()
                if wall < receipt[FIELD]['received_at']: raise _AdmissionDenied('clock_unavailable')
                ttl = max(0., 86400. - (wall - receipt[FIELD]['received_at']))
                # An independent concurrent response cannot rejuvenate an
                # already retained original mint initialization.
            if ttl <= 0: return receipt
            self._cache[name] = (now + ttl, deepcopy(receipt))
            self._cache.move_to_end(name)
            while len(self._cache) > self.cache_size: self._cache.popitem(last=False)
            return receipt

    async def _await_lookup(self, reference, endpoint):
        task = asyncio.create_task(self.lookup(deepcopy(reference), endpoint=endpoint,
            timeout_s=self.timeout_s), name='rpc-mint-birth-provider')
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.timeout_s)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
                except Exception: break
            try: task.result()
            except (asyncio.CancelledError, Exception): pass
            raise

    async def _lookup(self, reference, endpoint, source_hash, key):
        try:
            raw = await self._await_lookup(reference, endpoint)
            address = reference['address']
            wall = self._wall_now()
            proof = checked_token_birth(raw.get(FIELD), address, now=wall) if isinstance(raw, dict) else None
            if (proof is not None and (proof.get('source') != 'solana_rpc'
                    or proof.get('rpc_source_sha256') != source_hash or proof.get('reference') != reference)):
                proof = None
            result = merge_birth_context({'address':address}, raw, now=wall) if proof is not None else None
            if result is not None and FIELD not in result: result = None
            status = 'resolved' if result is not None else 'unavailable'
        except asyncio.TimeoutError:
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError from None
            result, status = None, 'timeout'
        except _AdmissionDenied:
            return {'receipt':None, 'status':'clock_unavailable'}
        except Exception:
            # Provider exception text may contain credentials or response data.
            result, status = None, 'provider_error'
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError from None
        try:
            original = self._remember(reference['address'], key, result)
            if original is not None and original != result:
                result, status = original, 'cache_hit'
        except _AdmissionDenied: return {'receipt':None, 'status':'clock_unavailable'}
        return {'receipt':result, 'status':status}

    async def enrich(self, token):
        token = deepcopy(token)
        if birth_clock(token) is not None: return self._finish(token, None, 'existing_original_clock')
        if any(token.get(name) is not None for name in BIRTH_CLOCK_FIELDS):
            return self._finish(token, None, 'invalid_existing_clock')
        try:
            wall = self._wall_now()
            reference = checked_reference(token.get(REFERENCE_FIELD), token.get('address'), now=wall)
            hit, cached = self._cached(token.get('address'))
        except _AdmissionDenied: return self._finish(token, None, 'clock_unavailable')
        if reference is None: return self._finish(token, None, 'missing_creation_reference')
        if hit and checked_token_birth(cached.get(FIELD), token.get('address'), now=wall) is not None:
            return self._finish(token, cached, 'cache_hit')
        try:
            endpoint = rpc.checked_endpoint(self.endpoint())
            source_hash = rpc.endpoint_fingerprint(endpoint)
        except Exception:
            return self._finish(token, None, 'not_configured')
        key = (reference['address'], reference['tx_hash'], reference['slot'], reference['received_at'], source_hash)
        try: hit, cached = self._cached(reference['address'], key)
        except _AdmissionDenied: return self._finish(token, None, 'clock_unavailable')
        if hit: return self._finish(token, cached, 'cache_hit' if cached is not None else 'negative_cache')
        try:
            degraded = self.degraded()
        except Exception: return self._finish(token, None, 'provider_state_unavailable')
        if type(degraded) is not bool: return self._finish(token, None, 'provider_state_unavailable')
        if degraded: return self._finish(token, None, 'provider_degraded')
        try:
            result = await self._requests.run(key,
                lambda: self._lookup(reference, endpoint, source_hash, key), on_admit=self._admit)
        except _AdmissionDenied as error:
            return self._finish(token, None, error.args[0])
        if result is None: return self._finish(token, None, 'concurrency_budget')
        return self._finish(token, result['receipt'], result['status'])
