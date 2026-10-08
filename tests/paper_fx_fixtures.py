"""Typed, clock-bound synthetic PAPER execution conversion; no provider."""
from unittest.mock import AsyncMock

from utils.sol_price import SolUsdObservation


def observation(stamp, price=100.):
    return SolUsdObservation("OK", price, stamp.timestamp(), stamp.timestamp(), reason="synthetic_fixture")


def install(monkeypatch, paper):
    async def read():
        return observation(paper.utc_now())
    reader = AsyncMock(side_effect=read)
    monkeypatch.setattr(paper, "_resolve_execution_fx", reader)
    return reader
