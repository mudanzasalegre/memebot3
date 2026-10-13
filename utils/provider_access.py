"""Fixed-resource authorization cooldown, detached diagnostics, no credentials out."""
from dataclasses import dataclass
import math
import threading
import time

from utils.bounded_state import bounded_int


@dataclass(frozen=True)
class AccessLease:
    resource: str
    configuration_generation: int
    request_generation: int
    recovery_probe: bool


class ResourceAccess:
    """401/403 belong to a credential/resource, not each requested token.

    Ordinary first requests keep existing concurrency. After denial only one
    recovery probe can run across threads/loops. Refresh does not bypass it.
    Latest completed resource generation owns health, not other tokens' caches.
    Admission alone cannot discard a denial received before a waiting GET.
    A changed in-memory credential invalidates old health and response leases;
    this class never reads or modifies credential files or refreshes them.
    """
    def __init__(self, resources, cooldown_s=300, *, clock=time.monotonic):
        self.resources = frozenset(resources)
        self.cooldown_s = bounded_int(cooldown_s, 300, 1, 3600)
        self.clock = clock
        self._lock = threading.RLock()
        self._credential = None
        self._configuration_generation = 0
        self._states = {}
        self._last_now = None

    def _sync(self, credential):
        if credential != self._credential:
            self._credential = credential
            self._configuration_generation += 1
            self._states.clear()
        try:
            now = self.clock()
            if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
                return None
        except Exception:
            return None
        if self._last_now is not None and now < self._last_now:
            for state in self._states.values():
                if state['denied']:
                    state['retry_at'] = now + self.cooldown_s
        for state in self._states.values():
            if state['denied'] and state['retry_at'] is None:
                state['retry_at'] = now + self.cooldown_s
        self._last_now = now
        return now

    @staticmethod
    def _state():
        return {'generation':0, 'completed_generation':0,
                'denied':False, 'retry_at':None, 'probe':None,
                'last_http_status':None, 'last_denied_http_status':None, 'available':False,
                'recovery_wait':False}

    def _view(self, resource, credential, now):
        state = self._states.get(resource, self._state())
        if resource not in self.resources:
            status = 'unsupported_resource'
        elif not credential:
            status = 'not_configured'
        elif now is None:
            status = 'clock_unavailable'
        elif state['probe'] is not None:
            status = 'access_probe_inflight'
        elif state['denied']:
            status = ('retry_due' if state['retry_at'] is not None and now >= state['retry_at'] else
                      'access_recovery_wait' if state['recovery_wait'] else 'access_denied')
        else:
            status = 'available' if state['available'] else 'unknown'
        retry = None if now is None or state['retry_at'] is None else max(0., state['retry_at'] - now)
        return {'status':status, 'last_http_status':state['last_http_status'],
                'last_denied_http_status':state['last_denied_http_status'],
                'retry_after_s':retry, 'configuration_generation':self._configuration_generation,
                'basis':'local_http_access_state_not_subscription_or_buy_permission'}

    def status(self, resource, credential):
        with self._lock:
            now = self._sync(credential)
            return self._view(resource, credential, now)

    def snapshot(self, credential):
        with self._lock:
            now = self._sync(credential)
            return {resource:self._view(resource, credential, now) for resource in sorted(self.resources)}

    def admit(self, resource, credential):
        with self._lock:
            now = self._sync(credential)
            if self._view(resource, credential, now)['status'] not in {'unknown','available','retry_due'}:
                return None
            state = self._states.setdefault(resource, self._state())
            state['generation'] += 1
            lease = AccessLease(resource, self._configuration_generation, state['generation'], state['denied'])
            if lease.recovery_probe:
                state['probe'] = lease
            return lease

    def is_current(self, lease, credential):
        with self._lock:
            self._sync(credential)
            return lease.configuration_generation == self._configuration_generation

    def wire_allowed(self, lease, credential):
        """A request waiting in throttle must observe a completed denial before GET."""
        with self._lock:
            now = self._sync(credential)
            state = self._states.get(lease.resource)
            return (now is not None and lease.configuration_generation == self._configuration_generation
                    and state is not None and (not state['denied'] or state['probe'] == lease))

    def finish(self, lease, http_status, *, valid_response=False):
        with self._lock:
            now = self._sync(self._credential)
            state = self._states.get(lease.resource)
            if (lease.configuration_generation != self._configuration_generation or state is None
                    or lease.request_generation < state['completed_generation']):
                return False
            # New work admitted but not yet answered is not success evidence.
            # Older answers still cannot overwrite a newer completed answer.
            state['completed_generation'] = lease.request_generation
            state['last_http_status'] = http_status if type(http_status) is int else None
            if state['last_http_status'] in (401, 403):
                state.update(denied=True, available=False, recovery_wait=False,
                             last_denied_http_status=http_status,
                             retry_at=None if now is None else now + self.cooldown_s)
            elif valid_response is True and state['last_http_status'] == 200:
                state.update(denied=False, available=True, recovery_wait=False, retry_at=None)
            elif lease.recovery_probe:
                state.update(recovery_wait=True, retry_at=None if now is None else now + self.cooldown_s)
            if state['probe'] == lease:
                state['probe'] = None
            return True

    def release(self, lease):
        """Cancelled/failed probes remain bounded; no detached timeout lease."""
        with self._lock:
            state = self._states.get(lease.resource)
            if (lease.configuration_generation == self._configuration_generation and state is not None
                    and state['probe'] == lease):
                now = self._sync(self._credential)
                state.update(probe=None, recovery_wait=True,
                             retry_at=None if now is None else now + self.cooldown_s)
