# memebot3/analytics/requeue_policy.py
from __future__ import annotations

from datetime import datetime, timezone
import math
import os
from typing import Tuple

from config.config import (
    MAX_AGE_DAYS,
    MAX_MARKET_CAP_USD,
    MIN_MARKET_CAP_USD,
    MIN_LIQUIDITY_USD,
    MIN_VOL_USD_24H,
    MIN_AGE_MIN,
)
from utils.time import is_in_trading_window, seconds_until_next_window, parse_iso_utc, utc_now

# â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€” Tabla de reglas globales â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”
# Cada tupla: (reason, {"max_attempts": int, "delay": segundos})
# Usa delay=None para delays calculados dinÃ¡micamente en runtime.
RULES = [
    ("out_of_window", {"max_attempts": 999_999, "delay": None}),
    ("too_young", {"max_attempts": 6, "delay": 90}),
    ("other", {"max_attempts": 4, "delay": 180}),
    ("age", {"max_attempts": 0, "delay": 0}),
    ("mcap_high", {"max_attempts": 0, "delay": 0}),
    ("mcap_low", {"max_attempts": 2, "delay": 120}),
    ("vol_low", {"max_attempts": 3, "delay": 120}),
    ("liq_low", {"max_attempts": 2, "delay": 120}),
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


def _to_float_or_none(x) -> float | None:
    try:
        if x is None:
            return None
        if isinstance(x, str) and not x.strip():
            return None
        v = float(x)
        if math.isnan(v):
            return None
        return v
    except Exception:
        return None


def _extract_created_at(token: dict) -> datetime | None:
    created = token.get("created_at") or token.get("createdAt") or token.get("created") or token.get("createdAtUtc")
    if isinstance(created, datetime):
        if created.tzinfo is None:
            return created.replace(tzinfo=timezone.utc)
        return created.astimezone(timezone.utc)

    if isinstance(created, str) and created.strip():
        dt = parse_iso_utc(created.strip())
        if dt:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

    pc = token.get("pairCreatedAt") or token.get("pair_created_at") or token.get("pairCreatedAtMs")
    pc_f = _to_float_or_none(pc)
    if pc_f:
        try:
            ts = pc_f / 1000.0 if pc_f > 1e11 else pc_f
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None
    return None


def _age_minutes(token: dict) -> float:
    created_at = _extract_created_at(token)
    if created_at is not None:
        return max(0.0, (utc_now() - created_at).total_seconds() / 60.0)

    raw_age = token.get("age_min") or token.get("age_minutes") or 0.0
    try:
        return float(raw_age)
    except Exception:
        return 0.0


# â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€” API principal â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”â€”
# Devuelve (should_requeue, backoff_sec, reason)
def decide(token: dict, attempts: int, first_seen: float) -> Tuple[bool, int, str]:
    """
    PolÃ­tica de re-encolado:
      â€¢ Si fuera de ventana horaria (y hay ventanas definidas) â†’ requeue hasta prÃ³xima ventana.
      â€¢ Cierra duro por edad absoluta y mcap alta.
      â€¢ Para mcap/vol/liq bajos aplica backoff y tope de reintentos (RULES).
      â€¢ Si nada aplica, usa â€œotherâ€ (anti-bucle, backoff 180 s).
    """
    del first_seen  # la polÃ­tica actual no lo necesita, pero mantenemos la firma

    # 0) Gate horario CONDICIONADO a .env (si no hay ventanas â†’ no gate)
    H = (os.getenv("TRADING_HOURS", "") or "").strip()
    E = (os.getenv("TRADING_HOURS_EXTRA", "") or "").strip()
    if H or E:
        if not is_in_trading_window():
            next_sec = seconds_until_next_window()
            delay_dyn = max(60, int(next_sec or 300))
            should, backoff = _apply_rule("out_of_window", attempts, delay_dyn)
            return should, backoff, "out_of_window"

    # 1) Edades / mÃ©tricas bÃ¡sicas
    age_min = _age_minutes(token)
    age_days = age_min / 1440.0

    if age_days > MAX_AGE_DAYS:
        return False, 0, "age"

    if age_min < MIN_AGE_MIN:
        missing_sec = max(0.0, (MIN_AGE_MIN - age_min) * 60.0)
        should, backoff = _apply_rule("too_young", attempts, int(missing_sec) or 90)
        return should, backoff, "too_young"

    liq = token.get("liquidity_usd")
    vol = token.get("volume_24h_usd")
    mcap = token.get("market_cap_usd")

    # 2) Market cap
    if mcap is not None and not (isinstance(mcap, float) and math.isnan(mcap)) and mcap > MAX_MARKET_CAP_USD:
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
        base_delay = 120 if liq >= MIN_LIQUIDITY_USD * 0.7 else 180
        should, backoff = _apply_rule("liq_low", attempts, base_delay)
        return should, backoff, "liq_low"

    # 5) Si ningÃºn filtro anterior aplica â†’ â€œotherâ€
    should, backoff = _apply_rule("other", attempts, 180)
    return should, backoff, "other"
