"""Confirmed RPC top-token-account concentration, not an ownership graph.

Token accounts can include pool vaults; False is not a wallet-cluster safety
certificate. Missing/error/contradictory evidence is None, never healthy.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
from typing import Any
from uuid import uuid4

import aiohttp

from config import HELIUS_RPC_URL
from utils.simple_cache import cache_get, cache_set
from utils.auxiliary_observation import observation, checked_auxiliary_observation, cluster_inputs_value, observation_clock

TIMEOUT = 4.
log = logging.getLogger("helius_cluster")


async def _rpc(method: str, params: list[Any]) -> dict | None:
    if not HELIUS_RPC_URL:
        return None
    request_id = uuid4().hex
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
            async with session.post(HELIUS_RPC_URL, json=payload) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                received = observation_clock()
        if (not isinstance(data, dict) or data.get("jsonrpc") != "2.0"
                or data.get("id") != request_id or "error" in data
                or not isinstance(data.get("result"), dict)):
            return None
        return {"result": data["result"], "received_at": received}
    except Exception as exc:
        log.debug("Concentration RPC unavailable (%s)", type(exc).__name__)
        return None


async def fetch_observation(address: str) -> dict:
    key = f"rpc:concentration:receipt:v1:{address}"
    if (hit := checked_auxiliary_observation(cache_get(key), address, "cluster")) is not None:
        return hit
    result = observation("cluster", address, reason="request_failed_or_unavailable")
    largest, supply = await asyncio.gather(
        _rpc("getTokenLargestAccounts", [address, {"commitment": "confirmed"}]),
        _rpc("getTokenSupply", [address, {"commitment": "confirmed"}]))
    try:
        if largest is not None and supply is not None:
            accounts, quantity = largest["result"], supply["result"]
            if not isinstance(accounts.get("value"), list) or not 1 <= len(accounts["value"]) <= 20:
                raise ValueError("Invalid largest-account population")
            inputs = {"accounts": [{k: acc[k] for k in ("address", "amount", "decimals")}
                                   for acc in accounts["value"]],
                "total_supply": quantity["value"]["amount"], "decimals": quantity["value"]["decimals"],
                "largest_slot": accounts["context"]["slot"], "supply_slot": quantity["context"]["slot"],
                "largest_received_at": largest["received_at"], "supply_received_at": supply["received_at"],
                "commitment": "confirmed"}
            value = cluster_inputs_value(inputs)
            if value is not None:
                candidate = observation("cluster", address, value, source="solana_rpc_confirmed",
                    observed_at=min(largest["received_at"], supply["received_at"]), inputs=inputs)
                result = checked_auxiliary_observation(candidate, address, "cluster") or result
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        pass
    cache_set(key, deepcopy(result), ttl=60 if result["value"] is not None else 15)
    return result


async def suspicious_cluster(token_mint: str) -> bool | None:
    return (await fetch_observation(token_mint))["value"]
