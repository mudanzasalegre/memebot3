# memebot3/analytics/filters.py
"""
Filtro de pares basado en reglas básicas (no-IA).

Cambios
───────
2025-08-03
• Nuevo corte «too-young»: si el token tiene < CFG.MIN_AGE_MIN minutos
  se devuelve **None** → el caller lo re-encola y se vuelve a validar
  más tarde.
2025-08-09
• Ajustes “suaves” para tokens descubiertos vía Pump.fun:
  - Liquidez mínima reducida (60% del umbral; piso 1000 USD)
  - Market cap máximo aumentado (×1.5)
  Se aplican solo cuando token.discovered_via == "pumpfun".
2025-07-26 …
"""

from __future__ import annotations

import logging
import math
from typing import Dict, Optional

import numpy as np

from config.config import (
    MAX_24H_VOLUME,
    MAX_AGE_DAYS,
    MAX_MARKET_CAP_USD,
    MIN_AGE_MIN,            # ←★★
    MIN_HOLDERS,
    MIN_LIQUIDITY_USD,
    MIN_MARKET_CAP_USD,
    MIN_VOL_USD_24H,
    MIN_SCORE_TOTAL,
)
from utils.time import utc_now

log = logging.getLogger("filters")

# ─────────────────────────── FILTRO DURO ────────────────────────────
def basic_filters(token: Dict) -> Optional[bool]:
    """
    True   → pasa el filtro duro
    False  → descartado definitivamente
    None   → “delay”: re-encolar y reintentar más tarde
    """
    sym = token.get("symbol", token["address"][:4])

    # 0) edad mínima ----------------------------------------------------------------
    age_min = token.get("age_min") or token.get("age_minutes")  # ya lo calcula sanitize
    if age_min is not None and not math.isnan(age_min) and age_min < MIN_AGE_MIN:
        log.debug("⏳ %s age %.1fm < %.1f → too_young", sym, age_min, MIN_AGE_MIN)
        return None                     # re-queue, todavía muy pronto

    # 1) antigüedad absoluta ---------------------------------------------------------
    created = token.get("created_at")
    if not created:
        log.debug("✗ %s sin created_at", sym)
        return False

    age_sec  = (utc_now() - created).total_seconds()
    age_days = age_sec / 86_400
    if age_days > MAX_AGE_DAYS:
        log.debug("✗ %s age %.1f d > %s", sym, age_days, MAX_AGE_DAYS)
        return False

    # 2) liquidez • volumen • market-cap --------------------------------------------
    liq   = token.get("liquidity_usd")
    vol24 = token.get("volume_24h_usd")
    mcap  = token.get("market_cap_usd")

    # — ajustes suaves para Pump.fun —
    is_pf        = token.get("discovered_via") == "pumpfun"
    min_liq_th   = MIN_LIQUIDITY_USD if not is_pf else max(1000.0, MIN_LIQUIDITY_USD * 0.6)
    max_mcap_th  = MAX_MARKET_CAP_USD if not is_pf else MAX_MARKET_CAP_USD * 1.5

    # 2.a —ventana de gracia 0-5 min—
    if age_sec < 300:
        # Dentro de los 5 min no aplicamos cortes estrictos de liq/vol/mcap.
        pass
    else:
        if liq is None or math.isnan(liq) or liq < min_liq_th:
            log.debug(
                "✗ %s liq %.0f < %.0f (umbral %s%s)",
                sym, liq or 0, min_liq_th,
                "PF" if is_pf else "STD",
                ""  # por claridad en el log
            )
            return False

        if (
            vol24 is None
            or math.isnan(vol24)
            or vol24 < MIN_VOL_USD_24H
            or vol24 > MAX_24H_VOLUME
        ):
            log.debug("✗ %s vol24h %.0f fuera rango (tras 5 min)", sym, vol24 or 0)
            return False

        # mcap: mantenemos el mínimo estándar y relajamos el máximo si es PF
        if (
            mcap is not None
            and not math.isnan(mcap)
            and (mcap < MIN_MARKET_CAP_USD or mcap > max_mcap_th)
        ):
            log.debug(
                "✗ %s mcap %.0f fuera rango [%s-%.0f]%s",
                sym, mcap, MIN_MARKET_CAP_USD, max_mcap_th, " (PF)" if is_pf else ""
            )
            return False

    # 2.b —tokens muy recientes con liq==0 → delay—
    if age_sec < 60 and (liq is None or math.isnan(liq) or liq == 0):
        log.debug("⏳ %s liq 0/NaN con age<60 s → requeue", sym)
        return None

    # 3) holders --------------------------------------------------------------------
    holders = token.get("holders", 0) or 0
    if holders == 0:
        swaps_5m = token.get("txns_last_5m", 0) or 0
        if swaps_5m == 0:
            log.debug("✗ %s holders=0 y 0 swaps – muy temprano", sym)
            return False
    elif holders < MIN_HOLDERS:
        log.debug("✗ %s holders %s < %s", sym, holders, MIN_HOLDERS)
        return False

    # 4) early sell-off --------------------------------------------------------------
    if age_sec < 600:
        sells = token.get("txns_last_5m_sells") or 0
        buys  = token.get("txns_last_5m")       or 0
        if (sells + buys) and sells / (sells + buys) > 0.7:
            pc5 = (
                token.get("priceChange", {}).get("m5")
                or token.get("price_change_5m")
                or 0
            )
            pc5_val = float(pc5) if pc5 else 0.0
            if pc5_val < 2:
                pc5_val *= 100.0
            if -5 < pc5_val < 5:
                log.debug("✗ %s >70%% ventas iniciales (precio estable)", sym)
                return False

    return True


# ───────────────────────── PUNTUACIÓN SUAVE ─────────────────────────
def total_score(tok: dict) -> int:
    """Suma de puntos ‘blandos’; se usa tras pasar el filtro duro."""
    score = 0
    score += 15 if tok.get("liquidity_usd", 0)   >= MIN_LIQUIDITY_USD * 2 else 0
    score += 20 if tok.get("volume_24h_usd", 0)  >= MIN_VOL_USD_24H * 3   else 0
    score += 10 if tok.get("holders", 0)         >= MIN_HOLDERS * 2       else 0
    score += 15 if tok.get("rug_score", 0)       >= 70                    else 0
    score += 15 if not tok.get("cluster_bad", 0)                          else 0
    score += 10 if tok.get("social_ok", 0)                                else 0
    score += 10 if not tok.get("insider_sig", 0)                          else 0
    return score


# ───────────────────── helper predicciones IA ───────────────────────
def ai_pred_to_filter(pred: float) -> bool:
    """Convierte probabilidad del modelo a corte booleano."""
    return pred >= MIN_SCORE_TOTAL / 100
