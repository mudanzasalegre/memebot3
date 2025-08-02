# memebot3/fetcher/geckoterminal.py
"""
Wrapper para la API pública de **GeckoTerminal** (CoinGecko DEX).

• Solo actúa como *fallback* cuando DexScreener no trae liquidez, precio, etc.
  y la variable de entorno `USE_GECKO_TERMINAL=true`.

• Dos helpers:
      get_token_data(network, address)                # síncrono  (requests)
      get_token_data_async(network, address, session) # asíncrono (aiohttp)

Ambos devuelven un dict **normalizado** con las mismas claves que produce
DexScreener (`sanitize_token_data` las reconoce al instante):

    {
        "price_usd":         float | None,
        "liquidity_usd":     float | None,
        "volume_24h_usd":    float | None,
        "market_cap_usd":    float | None,
    }

Rate-limit
──────────
Se reutiliza el bucket global `utils.rate_limiter.GECKO_LIMITER`
(~30 requests/min por defecto), así evitamos duplicar lógica.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional, TypedDict, cast

import requests
from config.config import GECKO_API_URL
from utils.rate_limiter import GECKO_LIMITER

try:
    import aiohttp  # noqa: WPS433 – lib externa opcional
except ImportError:                                   # pragma: no cover
    aiohttp = None  # type: ignore

logger = logging.getLogger(__name__)

# ────────────────────────── flags y constantes ──────────────────────────
USE_GECKO_TERMINAL = os.getenv("USE_GECKO_TERMINAL", "true").lower() == "true"
_BASE_URL = GECKO_API_URL.rstrip("/")

# ─────────────────────────────── tipos ──────────────────────────────────
class GTData(TypedDict, total=False):
    price_usd: float | None
    liquidity_usd: float | None
    volume_24h_usd: float | None
    market_cap_usd: float | None


# ─────────────────────── helpers de rate-limit (sync) ───────────────────
def _acquire_sync() -> None:
    """Bloquea hasta que haya token disponible en el *limiter* global."""
    while True:
        now = time.monotonic()

        # rellenado periódico
        if now - GECKO_LIMITER._last_reset >= GECKO_LIMITER.interval:      # noqa: SLF001
            GECKO_LIMITER._tokens = GECKO_LIMITER.max_calls                # noqa: SLF001
            GECKO_LIMITER._last_reset = now                                # noqa: SLF001

        if GECKO_LIMITER._tokens > 0:                                      # noqa: SLF001
            GECKO_LIMITER._tokens -= 1                                     # noqa: SLF001
            return

        time.sleep(GECKO_LIMITER._time_until_reset() + 0.01)               # noqa: SLF001


# ────────────────────────── API pública (sync) ──────────────────────────
def get_token_data(
    network: str,
    address: str,
    session: Optional[requests.Session] = None,
    timeout: int = 5,
) -> Optional[GTData]:
    """
    Llamada *síncrona* bloqueante a GeckoTerminal.
    Devuelve un dict canónico o ``None`` si error/404.
    """
    if not USE_GECKO_TERMINAL:
        return None

    _acquire_sync()

    url = _build_endpoint(network, address)
    headers = {"Accept": "application/json", "User-Agent": "memebot3/1.0"}
    sess = session or requests

    try:
        resp = sess.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 404:
            logger.debug("[GT] %s → 404 (no indexado)", url)
            return None
        resp.raise_for_status()
        attrs = resp.json()["data"]["attributes"]
    except Exception as exc:                                               # noqa: BLE001
        logger.warning("[GT] Error %s para %s…", exc, address[:6])
        return None

    return _parse_attributes(attrs)


# ────────────────────────── API pública (async) ─────────────────────────
async def get_token_data_async(
    network: str,
    address: str,
    session: Optional["aiohttp.ClientSession"] = None,
    timeout: int = 5,
) -> Optional[GTData]:
    """
    Versión asíncrona (aiohttp).  
    Si *aiohttp* no está instalado, delega en la versión síncrona.
    """
    if not USE_GECKO_TERMINAL:
        return None

    if aiohttp is None:                                                    # pragma: no cover
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, get_token_data, network, address, None, timeout
        )

    async with GECKO_LIMITER:
        url = _build_endpoint(network, address)
        headers = {"Accept": "application/json", "User-Agent": "memebot3/1.0"}

        async def _fetch(client: "aiohttp.ClientSession") -> Optional[dict]:
            try:
                async with client.get(url, headers=headers, timeout=timeout) as r:
                    if r.status == 404:
                        logger.debug("[GT] %s → 404 (no indexado)", url)
                        return None
                    r.raise_for_status()
                    return cast(dict, await r.json())
            except aiohttp.ClientError as exc:
                logger.warning("[GT] Error red %s", exc)
                return None

        client = session or aiohttp.ClientSession()
        try:
            j = await _fetch(client)
        finally:
            if session is None:
                await client.close()

    if not j:
        return None

    try:
        attrs = j["data"]["attributes"]
    except KeyError:
        logger.warning("[GT] JSON sin 'attributes' para %s…", address[:6])
        return None

    return _parse_attributes(attrs)


# ────────────────────────── helpers internos ────────────────────────────
def _build_endpoint(network: str, address: str) -> str:
    """
    Construye la URL correcta:

    • **Solana** → `/networks/solana/tokens/{address}`
      (el *mint* del token se puede consultar directamente).

    • Cadenas EVM → `/networks/{network}/tokens/{address}`
    """
    network = network.lower()
    if network == "solana":
        return f"{_BASE_URL}/networks/solana/tokens/{address}"
    return f"{_BASE_URL}/networks/{network}/tokens/{address}"


def _parse_attributes(attrs: dict) -> GTData:
    """
    Traduce la respuesta cruda de GeckoTerminal al esquema canónico usado
    en todo el bot (mismos nombres que produce DexScreener).
    """

    def _to_float(val: str | float | None) -> Optional[float]:
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    price = (
        attrs.get("base_token_price_usd")     # /pools
        or attrs.get("price_usd")             # /tokens
        or attrs.get("price_in_usd")
    )

    liquidity = (
        attrs.get("reserve_in_usd")           # /pools
        or attrs.get("total_reserve_in_usd")  # histórico
        or attrs.get("liquidity_in_usd")      # /tokens
    )

    mcap = attrs.get("market_cap_usd") or attrs.get("fdv_usd")

    # volumen 24 h puede venir como objeto {"h24": ...}
    vol_node = attrs.get("volume_usd")
    vol24 = vol_node.get("h24") if isinstance(vol_node, dict) else None

    return GTData(
        price_usd=_to_float(price),
        liquidity_usd=_to_float(liquidity),
        volume_24h_usd=_to_float(vol24),
        market_cap_usd=_to_float(mcap),
    )


__all__ = [
    "get_token_data",
    "get_token_data_async",
    "USE_GECKO_TERMINAL",
]
