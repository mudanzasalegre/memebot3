"""Shared bounded cash polling, with optional injected market diagnostics."""
from __future__ import annotations

import datetime as dt
import asyncio
import logging
from pathlib import Path
from typing import Any

from config.config import CFG
from research_loop import entry_gate_forward, runner_forward, forward_budget

_LOCK = asyncio.Lock()
log = logging.getLogger("paired_forward")


async def tick(*, root: Path | str, cfg: Any = None, now: dt.datetime | None = None,
               prices_func=None, quote_func=None, sol_price_func=None, fx_func=None) -> dict[str, Any]:
    cfg = CFG if cfg is None else cfg
    if (getattr(cfg, "DRY_RUN", False) is not True or not (
            getattr(cfg, "PAPER_ENTRY_RESEARCH_ENABLED", False) is True
            or getattr(cfg, "PAPER_RUNNER_RESEARCH_ENABLED", False) is True)):
        return {"status": "disabled"}
    async with _LOCK:
        project = Path(root).resolve()
        stamp = now or dt.datetime.now(dt.timezone.utc)
        path = project / "data/research/paired_forward_market_clock.json"
        if stamp.tzinfo is None or not path.resolve().is_relative_to(project):
            return {"status": "invalid_clock_scope"}
        clock = forward_budget.read(path)
        if clock is None and path.exists():
            return {"status": "corrupt_clock"}
        previous = forward_budget.time((clock or {}).get("last_tick_at"))
        if path.exists() and previous is None:
            return {"status": "corrupt_clock"}
        if previous is not None and (stamp - previous).total_seconds() < 60:
            return {"status": "throttled"}
        forward_budget.write(path, {"last_tick_at": stamp.isoformat()})
        tokens = runner_forward.active_tokens(root) | entry_gate_forward.active_tokens(root)
        # Registration in both banks shares this capacity, so this is not a
        # silent truncation of the declared research population.
        if len(tokens) > 128:
            prices = {}
            status = "shared_capacity_exceeded"
        else:
            from analytics.api_budget import provider_status
            try:
                # Both financial collectors value exact reverse-quote cash.
                # An extra spot HTTP request cannot establish that cash basis.
                prices = {} if not tokens or prices_func is None or provider_status("jupiter").get("degraded") else await prices_func(sorted(tokens))
                if not isinstance(prices, dict):
                    prices = {}
            except Exception:
                prices = {}
            status = "observed"
        async def shared_prices(requested):
            return {key: prices[key] for key in requested if key in prices}
        # With the real clock, fills must be timestamped after their network
        # await; only explicit synthetic clocks freeze a tick's timestamp.
        common = {"root": root, "cfg": cfg, "now": now, "prices_func": shared_prices,
                  "quote_func": quote_func, "sol_price_func": sol_price_func, "fx_func": fx_func}
        async def secondary(component):
            try:
                return await component.tick(**common)
            except Exception as exc:
                log.warning("Secondary paper collector unavailable: %s", type(exc).__name__)
                return {"status": "secondary_unavailable", "quote_calls": 0}
        runner = await secondary(runner_forward)
        entry = await secondary(entry_gate_forward)
        return {"status": status, "market_tokens": len(tokens), "runner": runner, "entry": entry,
                "extra_quote_calls": runner.get("quote_calls", 0) + entry.get("quote_calls", 0)}
