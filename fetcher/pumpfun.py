# memebot3/fetcher/pumpfun.py
"""
Pump Fun feed mediante Bitquery GraphQL v2 (polling HTTP).

Devuelve una lista de dicts **sanitizados** con la misma estructura que
los pares de DexScreener, de modo que _evaluate_and_buy() funciona igual.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import List, Dict, Any

import aiohttp

from config import BITQUERY_TOKEN, PUMPFUN_PROGRAM
from utils.data_utils import sanitize_token_data
from utils.simple_cache import cache_get, cache_set
from utils.time import utc_now

log = logging.getLogger("pumpfun")

# ──────────────────────── Config ────────────────────────
BITQUERY_URL = "https://streaming.bitquery.io/eap"          # endpoint v2 (EAP)
PROGRAM_ID   = PUMPFUN_PROGRAM or "insert_pumfun_program_id_here"
LIMIT        = 10        # nº de tokens a traer
CACHE_TTL    = 20        # seg. entre llamadas (rate-limit)

# ───────────── Query GraphQL v2 (HTTP POST) ─────────────
_QUERY = """
query ($prog: String!, $lim: Int!) {
  Solana {
    TokenSupplyUpdates(
      limit: { count: $lim }
      orderBy: { descending: Block_Time }
      where: {
        Instruction: {
          Program: { Address: { is: $prog }, Method: { is: "create" } }
        }
      }
    ) {
      Block { Time }
      Transaction { Signer }
      TokenSupplyUpdate {
        PostBalance
        Currency {
          MintAddress
          Name
          Symbol
          Decimals
          Uri
        }
      }
    }
  }
}
"""

# ───────────────── Helper interno ───────────────────────
async def _fetch_latest() -> List[Dict[str, Any]]:
    if not BITQUERY_TOKEN:
        log.debug("PumpFun deshabilitado (BITQUERY_TOKEN vacío)")
        return []

    headers = {
        "Authorization": f"Bearer {BITQUERY_TOKEN}",
        "Content-Type":  "application/json",
    }
    payload = {"query": _QUERY, "variables": {"prog": PROGRAM_ID, "lim": LIMIT}}

    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            BITQUERY_URL, json=payload, headers=headers, timeout=20
        ) as r:
            if r.status == 401:
                log.error("[PumpFun] 401 Unauthorized – revisa BITQUERY_TOKEN / plan")
                return []
            r.raise_for_status()
            data = await r.json()

    # ─── Manejo seguro de la respuesta ──────────────────
    # 1) Si Bitquery devuelve "errors":[…], regístralo.
    if err := data.get("errors"):
        log.warning("[PumpFun] GraphQL errors: %s", err)

    # 2) Protegemos la navegación usando dicts vacíos por defecto.
    result  = data.get("data") or {}
    solana  = result.get("Solana", {})
    updates = solana.get("TokenSupplyUpdates", []) or []

    # ─── Parseo → dicts MemeBot ─────────────────────────
    now = utc_now()
    out: List[Dict[str, Any]] = []

    for u in updates:
        try:
            cur   = u["TokenSupplyUpdate"]["Currency"]
            mint  = cur["MintAddress"]
            ts_iso = u["Block"]["Time"].rstrip("Z")
            ts    = dt.datetime.fromisoformat(ts_iso).replace(tzinfo=dt.timezone.utc)
        except (KeyError, ValueError) as e:
            log.debug("[PumpFun] salto update mal formado: %s", e)
            continue

        tok = {
            "address":        mint,
            "symbol":         cur["Symbol"][:16],
            "name":           cur["Name"],
            "created_at":     ts,
            # métricas dummy → las rellenará DexScreener
            "liquidity_usd":  0.0,
            "volume_24h_usd": 0.0,
            "holders":        0,
            # meta
            "discovered_via": "pumpfun",
            "age_minutes":    (now - ts).total_seconds() / 60.0,
            "creator":        u["Transaction"]["Signer"],
        }
        out.append(sanitize_token_data(tok))

    log.debug("[PumpFun] %d tokens nuevos", len(out))
    return out

# ───────────────── API pública ────────────────
async def get_latest_pumpfun() -> List[Dict[str, Any]]:
    """
    Devuelve lista de tokens recientes del launchpad Pump.fun (si hay).
    """
    # caching (reduce GraphQL calls)
    if (res := cache_get("pumpfun:latest")) is not None:
        return res
    updates = await _fetch_latest()
    cache_set("pumpfun:latest", updates, ttl=CACHE_TTL)
    return updates
