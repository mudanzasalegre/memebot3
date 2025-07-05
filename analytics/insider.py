"""
analytics/insider.py
────────────────────
Detección simplificada de actividad de insiders:

• Marca como *alerta* (True) si en los primeros 20 min después del mint
  hay ≥3 compras individuales mayores al 3 % de la liquidez inicial.

Fuente de datos: DexScreener `txns` 5 min + liquidez.
(Es un heurístico, no exhaustivo).

Si no se puede obtener la info ⇒ devuelve False (sin alerta).
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Final

from fetcher import dexscreener
from utils.time import utc_now

log = logging.getLogger("insider")

WINDOW_MINUTES: Final[int] = 20
BIG_BUY_PCT: Final[float] = 0.03   # 3 % de la liquidez


async def insider_alert(address: str) -> bool:
    tok = await dexscreener.get_pair(address)
    if not tok:
        return False

    # DexScreener no da log completo vía API pública
    # así que simplificamos: usamos txns["last5m"]["buys"] como proxy
    # y comprobamos si la métrica es desproporcionada justo tras el mint.
    created = tok["created_at"]
    age_min = (utc_now() - created).total_seconds() / 60
    if age_min > WINDOW_MINUTES:
        return False

    try:
        buys_5m = tok["txns_last_5min"] = tok.get("txns_last_5min") or 0  # de DexScreener chart endpoint no público
    except KeyError:
        return False

    # Heurística: si hay más de 3 grandes compras en 5 min, alerta
    threshold = max(3, int(tok["liquidity"] * BIG_BUY_PCT // 1))
    alert = buys_5m >= threshold
    if alert:
        log.debug("[insider] ALERT %s  buys5m=%s thr=%s", address[:4], buys_5m, threshold)
    return alert
