# fetcher/geckoterminal.py
"""
Wrapper para la API pública de GeckoTerminal (CoinGecko DEX).

• **Se usa solo como *fallback*** cuando DexScreener no trae liquidez
  o precio y `USE_GECKO_TERMINAL=true` en el .env.

• Expone dos helpers:
      get_token_data(network, address)                # síncrono  (requests)
      get_token_data_async(network, address, session) # asíncrono (aiohttp)

Ambos devuelven un dict normalizado:

    {
        "price_usd": float | None,
        "liq_usd":   int   | None,
        "vol24h":    int   | None,
        "mcap":      int   | None,
    }

Rate-limit
──────────
Se reutiliza el bucket **global** de `utils.rate_limiter.GECKO_LIMITER`
(~30 req/min por defecto) – así no duplicamos lógica.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional, TypedDict, cast

import requests
from utils.rate_limiter import GECKO_LIMITER         # ← único limitador
from config.config import GECKO_API_URL               # URL base configurable

try:
    import aiohttp  # noqa: WPS433 – lib externa opcional
except ImportError:                                  # pragma: no cover
    aiohttp = None  # type: ignore

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
#  Flags de entorno
# --------------------------------------------------------------------------- #
USE_GECKO_TERMINAL = os.getenv("USE_GECKO_TERMINAL", "true").lower() == "true"
_BASE_URL = GECKO_API_URL.rstrip("/")

# --------------------------------------------------------------------------- #
#  Tipos
# --------------------------------------------------------------------------- #
class GTData(TypedDict, total=False):
    price_usd: float | None
    liq_usd:   int   | None
    vol24h:    int   | None
    mcap:      int   | None


# --------------------------------------------------------------------------- #
#  Helper: adquisición sincrónica de token del RateLimiter
# --------------------------------------------------------------------------- #
def _acquire_sync() -> None:
    """
    Bloquea hasta que haya token disponible en ``GECKO_LIMITER``.

    Implementa la misma política que ``RateLimiter.acquire`` pero de
    forma sincrónica (se usa en el helper bloqueante).
    """
    while True:
        now = time.monotonic()
        # rellenado periódico
        if now - GECKO_LIMITER._last_reset >= GECKO_LIMITER.interval:  # noqa: SLF001
            GECKO_LIMITER._tokens = GECKO_LIMITER.max_calls            # noqa: SLF001
            GECKO_LIMITER._last_reset = now                            # noqa: SLF001

        if GECKO_LIMITER._tokens > 0:                                  # noqa: SLF001
            GECKO_LIMITER._tokens -= 1                                 # noqa: SLF001
            return

        time.sleep(GECKO_LIMITER._time_until_reset() + 0.01)           # noqa: SLF001


# --------------------------------------------------------------------------- #
#  Funciones públicas (sync / async)
# --------------------------------------------------------------------------- #
def get_token_data(
    network: str,
    address: str,
    session: Optional[requests.Session] = None,
    timeout: int = 5,
) -> Optional[GTData]:
    """
    Llamada *síncrona* (bloqueante) a GeckoTerminal.
    Devuelve dict normalizado o ``None`` si error/404.
    """
    if not USE_GECKO_TERMINAL:
        return None

    _acquire_sync()                           # ← rate-limit sincrónico

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
    except Exception as exc:                  # noqa: BLE001
        logger.warning("[GT] Error %s para %s", exc, address[:6])
        return None

    return _parse_attributes(attrs)


async def get_token_data_async(
    network: str,
    address: str,
    session: Optional["aiohttp.ClientSession"] = None,
    timeout: int = 5,
) -> Optional[GTData]:
    """
    Versión *asíncrona* (aiohttp). Si ``aiohttp`` no está instalado,
    delega en la llamada síncrona dentro de un *executor*.
    """
    if not USE_GECKO_TERMINAL:
        return None

    if aiohttp is None:                       # pragma: no cover
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, get_token_data, network, address, None, timeout
        )

    async with GECKO_LIMITER:                 # rate-limit *async*
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
        logger.warning("[GT] JSON sin attributes para %s", address[:6])
        return None

    return _parse_attributes(attrs)


# --------------------------------------------------------------------------- #
#  Helpers internos
# --------------------------------------------------------------------------- #
def _build_endpoint(network: str, address: str) -> str:
    """
    Devuelve la URL correcta según la red:

    * Para **Solana**: `/networks/solana/pools/{address}`
    * Para el resto (EVM): `/networks/{network}/tokens/{address}`
    """
    network = network.lower()
    if network == "solana":
        return f"{_BASE_URL}/networks/solana/pools/{address}"
    return f"{_BASE_URL}/networks/{network}/tokens/{address}"


def _parse_attributes(attrs: dict) -> GTData:
    """
    Convierte los atributos brutos de la API en el esquema estándar del bot.
    Maneja tanto la respuesta *pools* (Solana) como *tokens* (EVM).
    """

    def _to_int(val: str | None) -> Optional[int]:
        try:
            return int(float(val)) if val is not None else None
        except (TypeError, ValueError):
            return None

    # --- campos que cambian entre /pools y /tokens ----------
    price = (
        attrs.get("base_token_price_usd")
        or attrs.get("price_usd")
        or attrs.get("price_in_usd")
    )

    liq = (
        attrs.get("reserve_in_usd")
        or attrs.get("total_reserve_in_usd")
        or attrs.get("liquidity_in_usd")
    )

    mcap = attrs.get("fdv_usd") or attrs.get("market_cap_usd")

    vol_node = attrs.get("volume_usd")
    vol24 = None
    if isinstance(vol_node, dict):
        vol24 = vol_node.get("h24")

    return GTData(
        price_usd=float(price) if price else None,
        liq_usd=_to_int(liq),
        vol24h=_to_int(vol24),
        mcap=_to_int(mcap),
    )


__all__ = [
    "get_token_data",
    "get_token_data_async",
    "USE_GECKO_TERMINAL",
]
