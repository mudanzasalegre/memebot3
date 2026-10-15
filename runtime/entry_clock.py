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
                             max_wait_s=.1, max_reads=128, diagnostics=None):
    """A fresh receipt may precede datetime's microsecond floor in one tick.

    Wait for a separately observed UTC reading, never derive T0 from a receipt.
    A receipt currently in the numeric wall clock's future is rejected before
    waiting. Backwards/invalid clocks fail closed, including during catch-up.
    This bounds admitted reads/wait, not operating-system scheduling latency.
    Numeric/datetime conversion may disagree on the same native clock tick.
    Such an unconfirmed UTC is never accepted: try a new independently read
    pair inside the original bounds. Diagnostics are output only, never proof.
    """
    reads, ahead = 0, 0
    started = last_wall = last_mono = last_utc = None
    def finish(reason, observed=None):
        if type(diagnostics) is dict:
            diagnostics.clear()
            diagnostics.update(reason=reason, reads=reads, utc_ahead_retries=ahead,
                minimum_epoch=minimum_epoch if _number(minimum_epoch) else None,
                last_wall=last_wall if _number(last_wall) else None,
                last_utc=last_utc if _number(last_utc) else None,
                elapsed_s=last_mono-started if _number(last_mono) and _number(started) else None)
        return observed
    if (not _number(max_wait_s) or not 0 <= max_wait_s <= 1
            or type(max_reads) is not int or not 1 <= max_reads <= 1024):
        return finish('invalid_bounds')
    if minimum_epoch is not None and (not _number(minimum_epoch) or not 0 < minimum_epoch < 1e11):
        return finish('invalid_receipt')
    try:
        wall_before = wall()
        started = monotonic()
    except Exception:
        return finish('clock_read_error')
    if (not _number(wall_before) or wall_before <= 0 or not _number(started)
            or minimum_epoch is not None and minimum_epoch > wall_before):
        return finish('future_receipt' if _number(wall_before) and _number(minimum_epoch)
            and minimum_epoch > wall_before else 'invalid_initial_clock')
    last_wall, last_mono, last_utc = wall_before, started, None
    for _ in range(max_reads):
        try:
            elapsed_clock = monotonic()
            if not _number(elapsed_clock) or elapsed_clock < last_mono:
                return finish('invalid_monotonic')
            last_mono = elapsed_clock
            if elapsed_clock-started > max_wait_s:
                return finish('wait_limit')
            observed = utc()
            reads += 1
            if not isinstance(observed,dt.datetime) or observed.tzinfo is None or observed.utcoffset() is None:
                return finish('invalid_utc')
            epoch = observed.timestamp()
            wall_after = wall()
            if (not _number(epoch) or epoch <= 0 or not _number(wall_after)
                    or wall_after < last_wall
                    or last_utc is not None and epoch < last_utc):
                return finish('invalid_or_backwards_clock')
            last_wall, last_utc = wall_after, epoch
            if epoch <= wall_after and (minimum_epoch is None or epoch >= minimum_epoch):
                return finish('confirmed', observed)  # Exact just-observed object.
            if epoch > wall_after:
                ahead += 1
        except Exception:
            return finish('clock_read_error')
        if elapsed_clock-started >= max_wait_s:
            return finish('wait_limit')
        # Keep the production floor/ceiling predicates unchanged. A real clock
        # tick, not tolerance, resolves a representational ordering ambiguity.
        await sleep(min(.001,max_wait_s-(elapsed_clock-started)))
    return finish('read_limit')
