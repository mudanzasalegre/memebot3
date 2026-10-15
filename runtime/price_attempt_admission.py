"""Bounded local Pump.fun attempt admission, not wire quota or buy permission."""
from __future__ import annotations

from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
import math
import os
import threading
import time


def _count(value, default, maximum):
    try:
        if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
            return default
        return max(0, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _seconds(value, default, *, zero=False):
    try:
        number = float(value)
        if not isinstance(value, bool) and math.isfinite(number) and (number >= 0 if zero else number > 0):
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def retry_delay_seconds(value, *, default=5):
    """Whole-second queue wake-up, never earlier than a normal finite wait.

    Extreme configured waits are rechecked hourly, not admitted early: the
    monotonic admission owner still enforces their original expiry.
    """
    seconds = _seconds(value, default)
    return max(1, math.ceil(min(seconds, 3600.)))


@dataclass(frozen=True, slots=True)
class AttemptAdmission:
    allowed: bool
    reason: str
    retry_after_s: float


class PriceAttemptAdmission:
    """One process-wide owner for per-token cooldown and rolling attempts.

    Expired keys are swept; live cooldowns are never evicted to admit another
    token. The timestamp deque cannot silently discard the window's attempts.
    Invalid/backwards clocks fail closed without rejuvenating old state.
    """
    def __init__(self, *, quota=4, window_s=10., cooldown_s=25., capacity=8192,
                 clock=time.monotonic):
        self.quota = _count(quota, 4, 4096)
        self.window_s = _seconds(window_s, 10.)
        self.cooldown_s = _seconds(cooldown_s, 25., zero=True)
        self.capacity = max(1, _count(capacity, 8192, 65536))
        self.clock = clock
        self._attempts = deque()
        self._tokens = OrderedDict()
        self._last_clock = None
        self._counts = Counter()
        self._lock = threading.RLock()

    @classmethod
    def from_env(cls, environ=None, *, clock=time.monotonic):
        env = os.environ if environ is None else environ
        return cls(quota=env.get('PUMPFUN_PRICE_QUOTA', '4'),
                   window_s=env.get('PUMPFUN_PRICE_QUOTA_WINDOW', '10'),
                   cooldown_s=env.get('PUMPFUN_PRICE_ATTEMPT_COOLDOWN', '25'), clock=clock)

    def _now(self):
        try:
            now = self.clock()
            if (type(now) not in (int, float) or not math.isfinite(now) or now < 0
                    or self._last_clock is not None and now < self._last_clock
                    or not math.isfinite(now + self.window_s) or now + self.window_s <= now
                    or self.cooldown_s and (not math.isfinite(now + self.cooldown_s) or now + self.cooldown_s <= now)):
                return None
        except Exception:
            return None
        self._last_clock = now
        return now

    def _prune(self, now):
        while self._attempts and self._attempts[0] + self.window_s <= now:
            self._attempts.popleft()
        # Accepted timestamps are monotonic; all keys use the same cooldown.
        while self._tokens:
            key, expiry = next(iter(self._tokens.items()))
            if expiry > now:
                break
            self._tokens.pop(key)

    def _result(self, allowed, reason, retry=0.):
        self._counts[reason] += 1
        return AttemptAdmission(allowed, reason, retry)

    def admit(self, address):
        with self._lock:
            if (not isinstance(address, str) or not address or len(address) > 128
                    or any(character.isspace() for character in address)):
                return self._result(False, 'invalid_identity', 5.)
            now = self._now()
            if now is None:
                return self._result(False, 'clock_unavailable', 5.)
            self._prune(now)
            if not self.quota:
                return self._result(False, 'disabled', self.window_s)
            token_wait = max(0., self._tokens.get(address, now) - now)
            window_wait = (max(0., self._attempts[0] + self.window_s - now)
                           if len(self._attempts) >= self.quota else 0.)
            if token_wait or window_wait:
                reason = ('token_and_window_wait' if token_wait and window_wait else
                          'token_cooldown' if token_wait else 'window_capacity')
                return self._result(False, reason, max(token_wait, window_wait))
            if self.cooldown_s and len(self._tokens) >= self.capacity:
                retry = max(0., next(iter(self._tokens.values())) - now)
                return self._result(False, 'state_capacity', retry)
            self._attempts.append(now)
            if self.cooldown_s:
                self._tokens[address] = now + self.cooldown_s
            return self._result(True, 'admitted')

    def snapshot(self):
        with self._lock:
            now = self._now()
            if now is not None:
                self._prune(now)
            return {'quota': self.quota, 'window_s': self.window_s, 'cooldown_s': self.cooldown_s,
                    'capacity': self.capacity, 'cooling_tokens': len(self._tokens),
                    'attempts_in_window': len(self._attempts), 'counts': dict(self._counts),
                    'clock_available': now is not None,
                    'basis': 'local_attempt_admission_not_wire_quota_entitlement_or_buy_permission'}
