# memebot3/analytics/insider.py
"""
Detección simplificada de actividad de insiders (heurístico).

Regla (proxy):
  Dentro de los primeros WINDOW_MINUTES minutos tras el mint,
  dispara alerta si:
    • hay al menos MIN_BUYS_5M compras en los últimos 5 min, y
    • el precio sube ≥ MIN_PCT_UP_5M en 5 min, y
    • la liquidez es al menos MIN_LIQ_USD (para evitar ruido de pools ínfimos).

Notas:
  - No inspeccionamos tamaños de cada compra (la API pública no expone el tape completo).
  - Usamos `txns_last_5m` (buys) y `priceChange.m5` de DexScreener cuando está.
  - Si faltan datos críticos, devolvemos False (no alerta).
"""

from __future__ import annotations

import logging
from typing import Final, Optional

from fetcher import dexscreener
from utils.time import utc_now, parse_iso_utc

log = logging.getLogger("insider")

# Ventana de observación tras el mint
WINDOW_MINUTES: Final[int] = 20

# Umbrales del heurístico
MIN_BUYS_5M:     Final[int]   = 3      # mínimo de compras en 5m
MIN_PCT_UP_5M:   Final[float] = 5.0    # % mínimo de subida en 5m
MIN_LIQ_USD:     Final[float] = 3000.0 # liquidez mínima para considerar la señal

def _to_float(x) -> Optional[float]:
    try:
        v = float(x)
        # considéralo inválido si es NaN
        if v != v:  # NaN check
            return None
        return v
    except Exception:
        return None


async def insider_alert(address: str) -> bool:
    # 1) Snapshot DexScreener normalizado
    tok = await dexscreener.get_pair(address)
    if not tok:
        return False

    # 2) Edad desde created_at
    created = tok.get("created_at")
    if isinstance(created, str):
        created = parse_iso_utc(created)
    if not created:
        return False

    age_min = (utc_now() - created).total_seconds() / 60.0
    if age_min > WINDOW_MINUTES:
        return False

    # 3) Señales básicas disponibles
    buys_5m = tok.get("txns_last_5m") or 0
    try:
        buys_5m = int(buys_5m)
    except Exception:
        buys_5m = 0

    liq_usd = _to_float(tok.get("liquidity_usd"))

    # priceChange 5m (puede venir como 0.02 ó 2 → normalizamos a %)
    pc5_raw = (tok.get("priceChange") or {}).get("m5") if isinstance(tok.get("priceChange"), dict) else None
    if pc5_raw is None:
        pc5_raw = tok.get("price_change_5m")
    pc5 = _to_float(pc5_raw)
    if pc5 is not None and abs(pc5) < 1.0:
        pc5 *= 100.0  # interpretamos como fracción

    # 4) Reglas del heurístico (proxy)
    if liq_usd is None or pc5 is None:
        # Sin datos suficientes: no alertar
        return False

    if liq_usd < MIN_LIQ_USD:
        return False

    alert = (buys_5m >= MIN_BUYS_5M) and (pc5 >= MIN_PCT_UP_5M)

    if alert:
        log.debug(
            "[insider] ALERT %s buys5m=%s pc5=%.2f%% liq=%.0f age=%.1fm",
            address[:4], buys_5m, pc5, liq_usd, age_min
        )
    return bool(alert)
