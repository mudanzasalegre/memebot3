"""Bounded awaited creation lookup; no detached work, orders or queue ownership."""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from copy import deepcopy
import math
import os
import time

from analytics.api_budget import provider_status
from analytics.token_birth import FIELD, base58_size, birth_clock, checked_token_birth, merge_birth_context
from analytics.token_time import BIRTH_CLOCK_FIELDS
from fetcher import birdeye
from runtime.rpc_token_birth import RpcTokenBirthResolver
from analytics.mint_creation_reference import FIELD as REFERENCE_FIELD, checked_reference


def _number(value, default, low, high, *, integer=False):
    try:
        parsed = float(value)
        if isinstance(value, bool) or not math.isfinite(parsed):
            return default
        return int(max(low, min(high, parsed))) if integer else max(low, min(high, parsed))
    except (TypeError, ValueError, OverflowError):
        return default


class TokenBirthResolver:
    """Conservative creation-only reservations, separate from price-provider RPM.

    The owner awaits/cancels its lookup. Joiners share a Future, not a detached
    provider task. Failed/unknown birth stays unknown. No request is made while
    the provider circuit is degraded or no credential is configured.
    """
    def __init__(self, *, lookup=None, configured=None, degraded=None, rpm=12,
                 timeout_s=3., cache_size=2048, max_inflight=2, negative_ttl_s=30.,
                 clock=time.monotonic, wall=time.time, resource_access=None):
        native_lookup = lookup is None or lookup is birdeye.get_token_creation_info
        self.lookup = lookup or birdeye.get_token_creation_info
        self.resource_access = resource_access or (birdeye.creation_resource_status if native_lookup else lambda: {})
        self.configured = configured or birdeye.creation_lookup_configured
        self.degraded = degraded or (lambda: provider_status("birdeye")["degraded"])
        self.rpm = _number(rpm, 12, 1, 60, integer=True)
        self.timeout_s = _number(timeout_s, 3., .05, 8.)
        self.cache_size = _number(cache_size, 2048, 1, 10000, integer=True)
        self.max_inflight = _number(max_inflight, 2, 1, 8, integer=True)
        self.negative_ttl_s = _number(negative_ttl_s, 30., 1., 600.)
        self.clock, self.wall = clock, wall
        self._cache = OrderedDict()
        self._inflight = {}
        self._reservations = deque()
        self._next_request_at = None
        self._counts = {}

    def snapshot(self):
        return {"cache_size": len(self._cache), "inflight": len(self._inflight),
                "lookup_reservations": dict(self._counts), "creation_lookup_rpm": self.rpm,
                "max_creation_cu_per_minute": self.rpm * 35, "timeout_s": self.timeout_s,
                "basis": "local_creation_only_upper_bound_not_total_provider_budget_or_entitlement",
                "resource_access": self._access()}

    def _access(self):
        try:
            value = self.resource_access()
            return {key: value[key] for key in ('status','last_http_status','last_denied_http_status',
                    'retry_after_s','configuration_generation','basis') if key in value} if isinstance(value, dict) else {}
        except Exception:
            return {"status":"access_state_unavailable"}

    @staticmethod
    def _access_block(access):
        status = access.get('status')
        return status if status in {'access_denied','access_recovery_wait','access_probe_inflight',
                                     'clock_unavailable','access_state_unavailable'} else None

    def _finish(self, token, receipt, status):
        out = merge_birth_context(token, receipt or {}, now=self.wall())
        out["token_birth_enrichment"] = {"status": status, "source": "birdeye",
            "basis": "bounded_creation_lookup_not_buy_permission", "resource_access": self._access()}
        self._counts[status] = self._counts.get(status, 0) + 1
        return out

    def _remember(self, address, receipt):
        now = self.clock()
        ttl = (max(0., 86400. - (self.wall() - receipt[FIELD]["received_at"]))
               if receipt is not None else self.negative_ttl_s)
        if ttl <= 0:
            return
        self._cache[address] = (now + ttl, deepcopy(receipt), self._access().get('configuration_generation'))
        self._cache.move_to_end(address)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    async def _lookup_owned(self, address):
        """Do not abandon provider cleanup on repeated cancellation/timeout.

        Timeout is a cancellation request, not a promise that provider cleanup
        has finished at the deadline. The entry owner stays owned until actual
        acknowledgement; independent discovery/monitor loops are unaffected.
        """
        task = asyncio.create_task(self.lookup(address), name="mint-birth-provider-lookup")
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.timeout_s)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # Preserve the first cancellation; further stop signals
                    # cannot detach this task or interrupt its async cleanup.
                    continue
                except Exception:
                    break
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
            raise

    async def enrich(self, token):
        token = deepcopy(token)
        address = token.get("address")
        if not base58_size(address, 32):
            return self._finish(token, None, "invalid_identity")
        if birth_clock(token) is not None:
            return self._finish(token, None, "existing_original_clock")
        if any(token.get(name) is not None for name in BIRTH_CLOCK_FIELDS):
            return self._finish(token, None, "invalid_existing_clock")
        now = self.clock()
        for key, (expiry, _, _) in list(self._cache.items()):
            if expiry <= now:
                self._cache.pop(key, None)
        if cached := self._cache.get(address):
            _, receipt, generation = cached
            access = self._access()
            if receipt is not None:
                self._cache.move_to_end(address)
                return self._finish(token, receipt, "cache_hit")
            if blocked := self._access_block(access):
                return self._finish(token, None, blocked)
            if generation == access.get('configuration_generation'):
                self._cache.move_to_end(address)
                return self._finish(token, None, "negative_cache")
            self._cache.pop(address, None)
        if future := self._inflight.get(address):
            try:
                receipt = await asyncio.wait_for(asyncio.shield(future), timeout=self.timeout_s)
                return self._finish(token, receipt, "coalesced" if receipt is not None else
                                    self._access_block(self._access()) or "coalesced_unavailable")
            except asyncio.TimeoutError:
                return self._finish(token, None, "coalesced_timeout")
        if not self.configured():
            return self._finish(token, None, "not_configured")
        access = self._access()
        if blocked := self._access_block(access):
            return self._finish(token, None, blocked)
        if self.degraded():
            return self._finish(token, None, "provider_degraded")
        if len(self._inflight) >= self.max_inflight:
            return self._finish(token, None, "concurrency_budget")
        while self._reservations and now - self._reservations[0] >= 60.:
            self._reservations.popleft()
        if len(self._reservations) >= self.rpm or self._next_request_at is not None and now < self._next_request_at:
            return self._finish(token, None, "creation_budget")
        self._reservations.append(now)
        self._next_request_at = now + 60. / self.rpm
        future = asyncio.get_running_loop().create_future()
        self._inflight[address] = future
        try:
            try:
                raw = await self._lookup_owned(address)
                proof = checked_token_birth(raw.get(FIELD), address, now=self.wall()) if isinstance(raw, dict) else None
                receipt = merge_birth_context({"address": address}, raw, now=self.wall()) if proof is not None else None
                if receipt is not None and FIELD not in receipt:
                    receipt = None
                status = "resolved" if receipt is not None else "unavailable"
                if receipt is None:
                    after_access = self._access()
                    if access.get('configuration_generation') != after_access.get('configuration_generation'):
                        status = "configuration_changed"
                    else:
                        status = self._access_block(after_access) or status
            except asyncio.TimeoutError:
                receipt, status = None, "timeout"
            except Exception:
                # Do not expose provider exception strings or secrets.
                receipt, status = None, "provider_error"
            if status not in {'access_denied','access_recovery_wait','access_probe_inflight',
                              'clock_unavailable','access_state_unavailable','configuration_changed'}:
                self._remember(address, receipt)
            future.set_result(deepcopy(receipt))
            return self._finish(token, receipt, status)
        finally:
            if not future.done():
                future.cancel()
            self._inflight.pop(address, None)


GLOBAL_TOKEN_BIRTH_RESOLVER = TokenBirthResolver(
    rpm=os.getenv("BIRTH_ENRICHMENT_RPM", "12"),
    timeout_s=os.getenv("BIRTH_ENRICHMENT_TIMEOUT_S", "3"),
    cache_size=os.getenv("BIRTH_ENRICHMENT_CACHE_SIZE", "2048"),
    negative_ttl_s=os.getenv("BIRTH_ENRICHMENT_NEGATIVE_TTL_S", "30"),
)

GLOBAL_RPC_TOKEN_BIRTH_RESOLVER = RpcTokenBirthResolver(
    rpm=os.getenv("BIRTH_RPC_ENRICHMENT_RPM", "12"),
    timeout_s=os.getenv("BIRTH_RPC_ENRICHMENT_TIMEOUT_S", "3"),
    cache_size=os.getenv("BIRTH_RPC_ENRICHMENT_CACHE_SIZE", "2048"),
    max_inflight=os.getenv("BIRTH_RPC_ENRICHMENT_MAX_INFLIGHT", "2"),
    max_joiners=os.getenv("BIRTH_RPC_ENRICHMENT_MAX_JOINERS", "256"),
    negative_ttl_s=os.getenv("BIRTH_RPC_ENRICHMENT_NEGATIVE_TTL_S", "5"),
)


def token_birth_enrichment_snapshot():
    # Preserve existing Birdeye aggregate fields for operational consumers.
    return {**GLOBAL_TOKEN_BIRTH_RESOLVER.snapshot(),
            "rpc_initialization":GLOBAL_RPC_TOKEN_BIRTH_RESOLVER.snapshot()}


def _enabled(name):
    return os.getenv(name, "true").strip().lower() in {"true", "1", "yes", "on"}


def birth_enrichment_diagnostic(token):
    """Bounded local labels/presence only, never a receipt or buy permission."""
    statuses = {'resolved', 'cache_hit', 'existing_original_clock', 'invalid_existing_clock',
        'invalid_identity', 'missing_creation_reference', 'negative_cache', 'unavailable',
        'timeout', 'provider_error', 'provider_degraded', 'provider_state_unavailable',
        'not_configured', 'clock_unavailable', 'creation_budget', 'concurrency_budget',
        'coalesced', 'coalesced_unavailable', 'coalesced_timeout', 'access_denied',
        'access_recovery_wait', 'access_probe_inflight', 'access_state_unavailable',
        'configuration_changed'}
    def label(value, allowed):
        return value if isinstance(value, str) and value in allowed else 'unknown'
    token = token if isinstance(token, dict) else {}
    state = token.get('token_birth_enrichment')
    state = state if isinstance(state, dict) else {}
    source = label(state.get('source'), {'birdeye', 'solana_rpc'})
    rpc = state if source == 'solana_rpc' else state.get('rpc_initialization')
    rpc = rpc if isinstance(rpc, dict) else {}
    return {'schema_version': 1, 'source': source,
        'status': label(state.get('status'), statuses),
        'rpc_status': label(rpc.get('status'), statuses),
        'creation_reference_present': token.get(REFERENCE_FIELD) is not None,
        'birth_observation_present': token.get('token_birth_observation') is not None,
        'basis': 'local_status_and_presence_not_verified_birth_or_buy_permission'}


async def enrich_token_birth(token):
    if not _enabled("BIRTH_ENRICHMENT_ENABLED"):
        return deepcopy(token)
    out = deepcopy(token)
    if (_enabled("BIRTH_RPC_ENRICHMENT_ENABLED")
            and checked_reference(out.get(REFERENCE_FIELD), out.get('address')) is not None):
        out = await GLOBAL_RPC_TOKEN_BIRTH_RESOLVER.enrich(out)
        if birth_clock(out) is not None:
            return out
    elif _enabled("BIRTH_RPC_ENRICHMENT_ENABLED"):
        # A different feed need not repeat an untrusted creation reference to
        # reuse an already checked original mint receipt. This path is memory
        # only and cannot reserve budget or launch a provider lookup.
        cached = GLOBAL_RPC_TOKEN_BIRTH_RESOLVER.cached_birth(out)
        if cached is not None:
            return cached
    if _enabled("USE_BIRDEYE"):
        rpc_state = deepcopy(out.get('token_birth_enrichment'))
        out = await GLOBAL_TOKEN_BIRTH_RESOLVER.enrich(out)
        if rpc_state is not None and rpc_state.get('source') == 'solana_rpc':
            out['token_birth_enrichment']['rpc_initialization'] = rpc_state
    return out
