# utils/solana_rpc.py
"""
Wrapper asíncrono (con retry + back-off + cache) para Solana JSON-RPC 1.0.

Expone:
    • get_sol_balance(pubkey=None)  → balance en SOL
    • get_balance_lamports(pubkey)  → balance en lamports
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import aiohttp

from config.config import CFG
from utils.simple_cache import cache_get, cache_set

log = logging.getLogger("solana_rpc")

# ───────── parámetros de red / retries ─────────
_RPC_URL          = CFG.RPC_URL
_TIMEOUT          = 8           # seg.
_MAX_TRIES        = 3
_BACKOFF_START    = 1           # seg.

# TTL de caché en memoria
_BALANCE_TTL      = 15          # seg.


# ───────── RPC genérico con back-off ──────────
async def _rpc(method: str, params: list[Any]) -> Optional[Dict]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    backoff = _BACKOFF_START

    for attempt in range(_MAX_TRIES):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT)
            ) as s:
                async with s.post(_RPC_URL, json=payload) as r:
                    if r.status in {429, 500, 502, 503, 504}:
                        raise aiohttp.ClientResponseError(
                            r.request_info, (), status=r.status
                        )
                    if r.status != 200:
                        log.debug("[RPC] %s %s", method, await r.text())
                        return None
                    data = await r.json()
                    return data.get("result")
        except Exception as exc:  # noqa: BLE001
            log.debug("[RPC] %s (%s/%s) → %s", method, attempt + 1, _MAX_TRIES, exc)
            if attempt < _MAX_TRIES - 1:
                await asyncio.sleep(backoff)
                backoff *= 2
    return None


# ───────── helpers públicos ──────────
async def get_balance_lamports(pubkey: str) -> int:
    """
    Devuelve balance *en lamports* (int). Si hay error → 0.
    """
    ck = f"bal_lp:{pubkey}"
    if (hit := cache_get(ck)) is not None:
        return hit  # type: ignore[return-value]

    res = await _rpc("getBalance", [pubkey, {"commitment": "processed"}])
    lamports = int(res.get("value")) if res else 0
    cache_set(ck, lamports, ttl=_BALANCE_TTL)
    return lamports


async def get_sol_balance(pubkey: str | None = None) -> float:
    """
    Balance de una cuenta en **SOL**.  Si no se pasa `pubkey`
    se usa automáticamente `CFG.SOL_PUBLIC_KEY`.

    Devuelve 0.0 si no hay clave o en caso de error.
    """
    if pubkey is None or pubkey == "":
        pubkey = CFG.SOL_PUBLIC_KEY or ""
    if not pubkey:
        log.debug("[RPC] get_sol_balance sin pubkey definido")
        return 0.0

    ck = f"bal:{pubkey}"
    if (hit := cache_get(ck)) is not None:
        return hit  # type: ignore[return-value]

    lamports = await get_balance_lamports(pubkey)
    sol = lamports / 1e9
    cache_set(ck, sol, ttl=_BALANCE_TTL)
    return sol


# ───────── CLI de prueba ──────────
if __name__ == "__main__":  # pragma: no cover
    import sys

    async def _demo() -> None:
        pk = sys.argv[1] if len(sys.argv) > 1 else CFG.SOL_PUBLIC_KEY or ""
        if not pk:
            print("❌  Necesitas pasar public key o definir SOL_PUBLIC_KEY en .env")
            return
        bal = await get_sol_balance(pk)
        print(f"{pk[:4]}… balance: {bal:.3f} SOL")

    asyncio.run(_demo())
