# memebot3/analytics/requeue_policy.py
from __future__ import annotations

import math
import os
from typing import Tuple

from config.config import (
    MAX_AGE_DAYS,
    MAX_MARKET_CAP_USD,
    MIN_MARKET_CAP_USD,
    MIN_LIQUIDITY_USD,
    MIN_VOL_USD_24H,
    MIN_AGE_MIN,  # ← para too_young
)
from utils.time import is_in_trading_window, seconds_until_next_window

# —————————————————— Tabla de reglas globales ——————————————————
# Cada tupla: (reason, {"max_attempts": int, "delay": segundos})
# Usa delay=None para delays calculados dinámicamente en runtime.
RULES = [
    ("out_of_window", {"max_attempts": 999_999, "delay": None}),  # delay dinámico
    ("too_young",    {"max_attempts": 6,        "delay": 90}),
    ("other",        {"max_attempts": 4,        "delay": 180}),   # anti-bucle
    ("age",          {"max_attempts": 0,        "delay": 0}),
    ("mcap_high",    {"max_attempts": 0,        "delay": 0}),
    ("mcap_low",     {"max_attempts": 2,        "delay": 120}),
    ("vol_low",      {"max_attempts": 3,        "delay": 120}),
    ("liq_low",      {"max_attempts": 2,        "delay": 120}),
]

_RULES_MAP = {k: v for k, v in RULES}


def _apply_rule(reason: str, attempts: int, default_delay: int | None) -> Tuple[bool, int]:
    """
    Aplica la tabla RULES para (reason, attempts) y devuelve:
      (should_requeue, backoff_seconds)
    Si delay es None en RULES, usa default_delay calculado por el caller.
    """
    rule = _RULES_MAP.get(reason, _RULES_MAP["other"])
    max_attempts = rule["max_attempts"]
    if attempts >= max_attempts:
        return False, 0
    delay = rule["delay"]
    if delay is None:
        delay = int(default_delay or 0)
    return True, int(delay)


# —————————————————— API principal ————————————————————————
# Devuelve (should_requeue, backoff_sec, reason)
def decide(token: dict, attempts: int, first_seen: float) -> Tuple[bool, int, str]:
    """
    Política de re-encolado:
      • Si fuera de ventana horaria (y hay ventanas definidas) → requeue hasta próxima ventana.
      • Cierra duro por edad absoluta y mcap alta.
      • Para mcap/vol/liq bajos aplica backoff y tope de reintentos (RULES).
      • Si nada aplica, usa “other” (anti-bucle, backoff 180 s).
    """
    # 0) Gate horario CONDICIONADO a .env (si no hay ventanas → no gate)
    H = (os.getenv("TRADING_HOURS", "") or "").strip()
    E = (os.getenv("TRADING_HOURS_EXTRA", "") or "").strip()
    if H or E:
        # Solo aplicar el gate si existen ventanas definidas
        if not is_in_trading_window():
            next_sec = seconds_until_next_window()
            # Manejo seguro si seconds_until_next_window() devuelve None
            delay_dyn = max(60, int(next_sec or 300))
            should, backoff = _apply_rule("out_of_window", attempts, delay_dyn)
            return should, backoff, "out_of_window"

    # 1) Edades / métricas básicas
    raw_age = token.get("age_min") or token.get("age_minutes") or 0.0
    try:
        age_min = float(raw_age)
    except Exception:
        age_min = 0.0
    age_days = age_min / 1440.0

    if age_days > MAX_AGE_DAYS:
        # demasiado antiguo → cierre duro
        return False, 0, "age"

    # demasiado joven → backoff hasta alcanzar MIN_AGE_MIN
    if age_min < MIN_AGE_MIN:
        missing_sec = max(0.0, (MIN_AGE_MIN - age_min) * 60.0)
        should, backoff = _apply_rule("too_young", attempts, int(missing_sec) or 90)
        return should, backoff, "too_young"

    liq  = token.get("liquidity_usd")
    vol  = token.get("volume_24h_usd")
    mcap = token.get("market_cap_usd")

    # 2) Market cap
    if mcap is not None and not (isinstance(mcap, float) and math.isnan(mcap)) and mcap > MAX_MARKET_CAP_USD:
        # demasiado grande → cierre duro
        return False, 0, "mcap_high"

    if mcap is not None and not (isinstance(mcap, float) and math.isnan(mcap)) and mcap < MIN_MARKET_CAP_USD:
        should, backoff = _apply_rule("mcap_low", attempts, 120)
        return should, backoff, "mcap_low"

    # 3) Volumen 24h
    if vol is not None and not (isinstance(vol, float) and math.isnan(vol)) and vol < MIN_VOL_USD_24H:
        should, backoff = _apply_rule("vol_low", attempts, 120)
        return should, backoff, "vol_low"

    # 4) Liquidez
    if liq is not None and not (isinstance(liq, float) and math.isnan(liq)) and liq < MIN_LIQUIDITY_USD:
        # severidad extra si <70% del mínimo → respeta max_attempts de RULES igualmente
        base_delay = 120 if liq >= MIN_LIQUIDITY_USD * 0.7 else 180
        should, backoff = _apply_rule("liq_low", attempts, base_delay)
        return should, backoff, "liq_low"

    # 5) Si ningún filtro anterior aplica → “other”
    should, backoff = _apply_rule("other", attempts, 180)
    return should, backoff, "other"
