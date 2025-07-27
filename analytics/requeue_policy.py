# memebot3/analytics/requeue_policy.py
from __future__ import annotations

import math
from typing import Tuple

from config.config import (
    MAX_AGE_DAYS,
    MAX_MARKET_CAP_USD,
    MIN_MARKET_CAP_USD,
    MIN_LIQUIDITY_USD,
    MIN_VOL_USD_24H,
)

# —————————————————— Tabla de reglas globales ——————————————————
# Cada tupla:   (reason, {"max_attempts": int, "delay": segundos})
# Añadimos “other” para evitar bucles infinitos ─ Mod 26-Jul-2025
RULES = [
    ("other",      {"max_attempts": 4, "delay": 180}),  # ← NUEVA
    ("age",        {"max_attempts": 0, "delay":   0}),
    ("mcap_high",  {"max_attempts": 0, "delay":   0}),
    ("mcap_low",   {"max_attempts": 2, "delay": 120}),
    ("vol_low",    {"max_attempts": 3, "delay": 120}),
    ("liq_low",    {"max_attempts": 2, "delay": 120}),
]

# —————————————————— API principal ————————————————————————
# Devuelve (should_requeue, backoff_sec, reason)
def decide(token: dict, attempts: int, first_seen: float) -> Tuple[bool, int, str]:
    age_min = float(token.get("age_minutes", 0))
    age_days = age_min / 1440.0
    liq     = token.get("liquidity_usd")
    vol     = token.get("volume_24h_usd")
    mcap    = token.get("market_cap_usd")

    if age_days > MAX_AGE_DAYS:
        return False, 0, "age"

    if mcap is not None and not math.isnan(mcap) and mcap > MAX_MARKET_CAP_USD:
        return False, 0, "mcap_high"

    if mcap is not None and not math.isnan(mcap) and mcap < MIN_MARKET_CAP_USD:
        if attempts >= 2 or age_min >= 10:
            return False, 0, "mcap_low"
        return True, 120, "mcap_low"

    if vol is not None and not math.isnan(vol) and vol < MIN_VOL_USD_24H:
        if attempts >= 3 or age_min >= 30:
            return False, 0, "vol_low"
        return True, 120, "vol_low"

    if liq is not None and not math.isnan(liq) and liq < MIN_LIQUIDITY_USD:
        if attempts >= 2 and liq < MIN_LIQUIDITY_USD * 0.7:
            return False, 0, "liq_low"
        return True, 120, "liq_low"

    # Si ningún filtro anterior aplica → “other”
    # Con la nueva regla: máximo 4 reintentos, 180 s cada uno
    return True, 180, "other"
