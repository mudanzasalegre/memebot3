"""
Filtro de pares basado en reglas básicas (no-IA).
"""
from __future__ import annotations

import logging
from typing import Dict

from config.config import (
    MAX_24H_VOLUME,
    MAX_AGE_DAYS,
    MIN_HOLDERS,
    MIN_LIQUIDITY_USD,
    MIN_VOL_USD_24H,
    MIN_SCORE_TOTAL,
)
from utils.time import utc_now

log = logging.getLogger("filters")

# ─────────────────────────── FILTRO DURO ────────────────────────────
def basic_filters(token: dict) -> bool:
    """
    True si el par pasa todos los filtros «duros».
    Si falla alguno ⇒ False (y se deja traza en log).
    """
    sym = token.get("symbol", token["address"][:4])

    # 1) antigüedad
    created = token.get("created_at")
    if not created:
        log.debug("✗ %s sin created_at", sym)
        return False

    age_sec  = (utc_now() - created).total_seconds()
    age_days = age_sec / 86_400
    if age_days > MAX_AGE_DAYS:
        log.debug("✗ %s age %.1f d > %s", sym, age_days, MAX_AGE_DAYS)
        return False

    # 2) liquidez
    liq = token.get("liquidity_usd", 0.0)
    if liq < MIN_LIQUIDITY_USD:
        log.debug("✗ %s liq %.0f < %s", sym, liq, MIN_LIQUIDITY_USD)
        return False

    # 3) volumen 24 h
    vol24 = token.get("volume_24h_usd", 0.0)
    if vol24 < MIN_VOL_USD_24H or vol24 > MAX_24H_VOLUME:
        log.debug("✗ %s vol24h %.0f fuera rango", sym, vol24)
        return False

    # 4) holders  ───────── parche: permitimos holders==0 si ya hubo swaps
    holders = token.get("holders", 0)
    if holders == 0:                                   # aún sin indexar
        swaps_5m = token.get("txns_last_5min", 0) or 0
        if swaps_5m == 0:                              # ni un swap aún → demasiado pronto
            log.debug("✗ %s holders=0 y 0 swaps – muy temprano", sym)
            return False
        # si hay swaps se omite el check de MIN_HOLDERS
    elif holders < MIN_HOLDERS:
        log.debug("✗ %s holders %s < %s", sym, holders, MIN_HOLDERS)
        return False

    # 5) early sell-off (dump en los 10 primeros min)
    if age_sec < 600:
        sells = token.get("txns_last_5min_sells") or 0
        buys  = token.get("txns_last_5min") or 0
        if (sells + buys) and sells / (sells + buys) > 0.7:
            pc5 = (token.get("priceChange", {}).get("m5")
                   or token.get("price_change_5m") or 0)
            pc5_val = float(pc5) if pc5 else 0.0
            if pc5_val < 2:           # DexScreener a veces da 0.01 ⇒ %
                pc5_val *= 100.0
            if -5 < pc5_val < 5:      # precio casi plano
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
    """Umbral fijo del modelo (0-1)."""
    return pred >= MIN_SCORE_TOTAL / 100
