"""Legacy API for a buying-momentum proxy, NOT confirmed insider activity.

Only actual buys and percentage-point changes from an original fresh snapshot
are used. Missing evidence returns None, not a fabricated clean risk result.
"""
from __future__ import annotations

from utils.auxiliary_observation import accumulation_observation, checked_auxiliary_observation


async def insider_alert(address: str, *, snapshot: dict | None = None) -> bool | None:
    if snapshot is None:
        from fetcher import dexscreener
        snapshot = await dexscreener.get_pair(address, force_refresh=True)
    if not isinstance(snapshot, dict) or snapshot.get("address") != address:
        return None
    record = checked_auxiliary_observation(accumulation_observation(snapshot), address, "early_accumulation")
    return record["value"] if record is not None else None
