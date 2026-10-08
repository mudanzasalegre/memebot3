"""Run the real shared-budget logic on a deterministic clock, without HTTP."""
import asyncio
import threading

from utils import jupiter_access

REAL_SLEEP = asyncio.sleep


def isolate_budget(monkeypatch):
    now = [0.0]
    lock = threading.Lock()

    def clock():
        with lock:
            return now[0]

    async def sleep(delay):
        with lock:
            now[0] += delay
        await REAL_SLEEP(0)

    budget = jupiter_access.AccessBudget(clock=clock,
        wall=lambda: 1_800_000_000 + clock(), sleep=sleep)
    monkeypatch.setattr(jupiter_access, "_BUDGET", budget)
    monkeypatch.setenv("JUP_API_RPS", "")
    return budget
