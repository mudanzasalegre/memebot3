"""Observe a real causal UTC time; never clamp a clock or add tolerance."""
import asyncio
import datetime as dt
import math
import time


def _utc_now():
    return dt.datetime.now(dt.timezone.utc)


def _number(value):
    try:
        return type(value) in (int,float) and math.isfinite(value)
    except (ValueError,TypeError,OverflowError):
        return False


async def capture_causal_utc(*, minimum_epoch=None, utc=_utc_now, wall=time.time,
                             monotonic=time.monotonic, sleep=asyncio.sleep,
                             max_wait_s=.1, max_reads=128):
    """A fresh receipt may precede datetime's microsecond floor in one tick.

    Wait for a separately observed UTC reading, never derive T0 from a receipt.
    A receipt currently in the numeric wall clock's future is rejected before
    waiting. Backwards/invalid clocks fail closed, including during catch-up.
    This bounds admitted reads/wait, not operating-system scheduling latency.
    """
    if (not _number(max_wait_s) or not 0 <= max_wait_s <= 1
            or type(max_reads) is not int or not 1 <= max_reads <= 1024):
        return None
    if minimum_epoch is not None and (not _number(minimum_epoch) or not 0 < minimum_epoch < 1e11):
        return None
    try:
        wall_before = wall()
        started = monotonic()
    except Exception:
        return None
    if (not _number(wall_before) or wall_before <= 0 or not _number(started)
            or minimum_epoch is not None and minimum_epoch > wall_before):
        return None
    last_wall, last_mono, last_utc = wall_before, started, None
    for _ in range(max_reads):
        try:
            elapsed_clock = monotonic()
            if (not _number(elapsed_clock) or elapsed_clock < last_mono
                    or elapsed_clock-started > max_wait_s):
                return None
            last_mono = elapsed_clock
            observed = utc()
            if not isinstance(observed,dt.datetime) or observed.tzinfo is None or observed.utcoffset() is None:
                return None
            epoch = observed.timestamp()
            wall_after = wall()
            if (not _number(epoch) or epoch <= 0 or not _number(wall_after)
                    or wall_after < last_wall or epoch > wall_after
                    or last_utc is not None and epoch < last_utc):
                return None
            last_wall, last_utc = wall_after, epoch
            if minimum_epoch is None or epoch >= minimum_epoch:
                return observed  # Exact object just observed; never a synthesized time.
        except Exception:
            return None
        if elapsed_clock-started >= max_wait_s:
            return None
        # Keep the production floor/ceiling predicates unchanged. A real clock
        # tick, not tolerance, resolves a representational ordering ambiguity.
        await sleep(min(.001,max_wait_s-(elapsed_clock-started)))
    return None
