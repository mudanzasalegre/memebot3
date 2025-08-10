"""
Scraper ultra-ligero de DexScreener que lista los *mint-addresses*
más recientes en Solana.

• Devuelve edad = 0.0 si no hay timestamp (deja que el filtro final decida).
• Filtra sólo por antigüedad aquí; el resto de reglas están en analytics.filters.
• CFG.MAX_CANDIDATES recorta la lista para no procesar más de N tokens.
• (NUEVO) Bloqueo de direcciones NO-Solana (0x…) y verificación opcional de chainId.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import List, Optional

import aiohttp

from utils.time import utc_now
from config import DEX_API_BASE, MAX_AGE_DAYS  # alias retro-compat
from config.config import CFG  # ← incluye MAX_CANDIDATES

log = logging.getLogger("descubridor")
log.setLevel(logging.DEBUG)

# ───────────────────────── endpoints ──────────────────────────
DEX = DEX_API_BASE.rstrip("/")
URLS = [
    f"{DEX}/token-profiles/latest/v1?chainId=solana&limit=500",
    f"{DEX}/latest/dex/pairs/solana?limit=500",
]

# ─────────────────────── helpers HTTP/JSON ─────────────────────
async def _json(s: aiohttp.ClientSession, url: str):
    try:
        timeout_s = getattr(CFG, "DEX_HTTP_TIMEOUT", 20)
        async with s.get(
            url,
            timeout=timeout_s,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        ) as r:
            if r.status == 404:
                return None
            r.raise_for_status()
            return await r.json()
    except Exception:  # noqa: BLE001
        return None


def _items(raw) -> list:
    """DexScreener suele envolver la lista en distintas claves."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for k in ("pairs", "tokens", "dexTokens", "data"):
            v = raw.get(k)
            if isinstance(v, list):
                return v
    return []


def _calc_age_days(tok: dict) -> float:
    """Calcula edad en días a partir de varios posibles campos timestamp."""
    ts_any = tok.get("listedAt") or tok.get("createdAt") or tok.get("pairCreatedAt")
    if ts_any is not None:
        try:
            ts = float(ts_any)
            if ts > 1e12:  # epoch ms → s
                ts /= 1000.0
            created = dt.datetime.utcfromtimestamp(ts)
            return (utc_now() - created).total_seconds() / 86400.0
        except Exception:  # noqa: BLE001
            pass

    # Si el endpoint ya trae la edad:
    for fld in ("ageDays", "age"):
        try:
            return float(tok.get(fld))
        except Exception:  # noqa: BLE001
            continue

    # Sin dato → asumimos 0 d para no filtrar aquí
    return 0.0


def _extract_addr(tok: dict) -> Optional[str]:
    """
    Intenta extraer el mint address desde varios formatos de entrada.
    Orden de prioridad:
      tokenAddress → baseToken.address → address → baseToken > address anidados
    """
    addr = (
        tok.get("tokenAddress")
        or (tok.get("baseToken") or {}).get("address")
        or tok.get("address")
    )
    if addr:
        return str(addr).strip()
    # búsqueda defensiva en estructuras raras
    base = tok.get("baseToken") or tok.get("base") or {}
    if isinstance(base, dict) and base.get("address"):
        return str(base["address"]).strip()
    return None


def _is_solana_address(addr: str) -> bool:
    """
    Check defensivo:
      • descarta EVM (0x…)
      • rango típico de longitud base58 (no estrictamente validado)
    """
    if not addr or addr.startswith("0x"):
        return False
    return 30 <= len(addr) <= 50


def _is_chain_solana(tok: dict) -> bool:
    """Si viene chainId, debe ser 'solana'. Si no viene, no bloquea."""
    cid = tok.get("chainId") or tok.get("chain") or tok.get("chainIdShort")
    if cid is None:
        return True
    # Normalizamos
    cid = str(cid).lower()
    return cid in ("solana", "sol")


# ───────────────────────── API pública ─────────────────────────
async def fetch_candidate_pairs() -> List[str]:
    """
    Devuelve mint-addresses con:
      • chainId == solana (si viene)
      • address con pinta de Solana (no 0x…)
      • edad ≤ MAX_AGE_DAYS
    y recorta por CFG.MAX_CANDIDATES conservando el orden.
    """
    async with aiohttp.ClientSession() as s:
        raw = None
        for u in URLS:
            raw = await _json(s, u)
            if raw:
                log.debug("DexScreener OK → %s", u.split(DEX, 1)[-1])
                break
        if not raw:
            log.error("DexScreener: ningún endpoint disponible")
            return []

    out: list[str] = []
    seen = set()

    max_candidates = int(getattr(CFG, "MAX_CANDIDATES", 0) or 0)

    for t in _items(raw):
        # 1) chainId (si existe)
        if not _is_chain_solana(t):
            # Evita ruido en logs: solo traza en DEBUG
            log.debug("⛔ chainId≠solana (descartado)")
            continue

        # 2) address
        addr = _extract_addr(t)
        if not addr:
            continue

        if not _is_solana_address(addr):
            log.debug("⛔ no-Solana (0x…/len) %s…", addr[:6])
            continue

        # 3) edad
        age = _calc_age_days(t)
        log.debug("⏱ %s… age=%.2f d", addr[:6], age)

        if age > MAX_AGE_DAYS:
            continue

        # 4) de-dup ordenado
        if addr in seen:
            continue
        seen.add(addr)
        out.append(addr)

        # 5) tope de candidatos
        if max_candidates and len(out) >= max_candidates:
            break

    log.info("Descubridor: %s candidatos (≤ %sd, tope=%s)",
             len(out), MAX_AGE_DAYS, (max_candidates or "∞"))
    return out


# ——— CLI rápido ———
if __name__ == "__main__":  # pragma: no cover
    async def _demo():
        lst = await fetch_candidate_pairs()
        print(len(lst), lst[:10])

    asyncio.run(_demo())
