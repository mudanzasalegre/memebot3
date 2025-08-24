# memebot3/analytics/filters.py
"""
Filtro de pares basado en reglas básicas (no-IA).

Cambios
───────
2025-08-24
• Gate horario condicionado a .env:
  Si TRADING_HOURS y TRADING_HOURS_EXTRA están vacíos → no se filtra por hora.
  Si alguna está definida → se usa is_in_trading_window() (config).

2025-08-21
• Gate por ventana horaria: si `is_in_trading_window()` es False, devuelve
  None (re-encolar) y no deja pasar señales fuera de horario.
  Usa config.TRADING_WINDOWS (parseado en utils/time.py vía config).

2025-08-10
• Filtro de RED al inicio: descarta direcciones no-Solana (0x…) y chainId≠solana.
• Normalización defensiva de símbolo y dirección para logs.
• Mantiene ventana de gracia temprana y ajustes “suaves” para Pump.fun.

2025-08-09
• Ajustes “suaves” para tokens descubiertos vía Pump.fun:
  - Liquidez mínima reducida (60% del umbral; piso 1000 USD)
  - Market cap máximo aumentado (×1.5)
  Se aplican solo cuando token.discovered_via == "pumpfun".

2025-08-03
• Nuevo corte «too-young»: si el token tiene < CFG.MIN_AGE_MIN minutos
  se devuelve **None** → el caller lo re-encola y se vuelve a validar
  más tarde.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import timezone
from typing import Dict, Optional

import numpy as np  # puede usarse en otras fases (mantenemos import)

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
from utils.time import utc_now, is_in_trading_window, parse_iso_utc

log = logging.getLogger("filters")


# ──────────────────────── helpers de red/formato ───────────────────────
def _extract_address(token: Dict) -> str | None:
    """
    Extrae el mint address desde varias claves posibles.
    """
    addr = (
        token.get("address")
        or token.get("token_address")
        or token.get("tokenAddress")
        or token.get("mint")
        or (token.get("baseToken") or {}).get("address")
    )
    return str(addr).strip() if addr else None


def _is_solana_address(addr: str) -> bool:
    """
    Check defensivo rápido:
      • descarta EVM (0x…)
      • rango típico de longitud base58 (~32–44; dejamos margen 30–50)
    No valida estrictamente base58 para no penalizar edge-cases.
    """
    if not addr or addr.startswith("0x"):
        return False
    return 30 <= len(addr) <= 50


def _is_chain_solana(token: Dict) -> bool:
    """
    Si viene chainId/chain, debe ser 'solana' (o 'sol').
    Si no viene, no bloqueamos por compatibilidad con distintas fuentes.
    """
    cid = token.get("chainId") or token.get("chain") or token.get("chainIdShort")
    if cid is None:
        return True
    cid = str(cid).lower()
    return cid in ("solana", "sol")


# ─────────────────────────── FILTRO DURO ────────────────────────────
def basic_filters(token: Dict) -> Optional[bool]:
    """
    True   → pasa el filtro duro
    False  → descartado definitivamente
    None   → “delay”: re-encolar y reintentar más tarde
    """
    addr = _extract_address(token)
    sym = token.get("symbol") or (addr[:4] if addr else "????")

    # -1) gate horario condicionado por .env:
    #     Si TRADING_HOURS y TRADING_HOURS_EXTRA están vacíos → no filtrar por hora.
    #     Si alguna está definida → aplicar check con is_in_trading_window().
    H = (os.getenv("TRADING_HOURS", "") or "").strip()
    E = (os.getenv("TRADING_HOURS_EXTRA", "") or "").strip()
    if H or E:
        # Sólo si hay ventanas definidas en .env
        if not is_in_trading_window():
            log.debug("⏸ %s fuera de ventana horaria → requeue", sym)
            return None
    # else: sin ventanas → no gate horario

    # 0) red: sólo Solana -----------------------------------------------------------
    if not _is_chain_solana(token):
        log.debug("✗ %s chainId≠solana (descartado)", sym)
        return False

    if not addr or not _is_solana_address(addr):
        log.debug("✗ %s address no-Solana/incorrecta (%r)", sym, addr)
        return False

    # 1) edad mínima ----------------------------------------------------------------
    age_min = token.get("age_min") or token.get("age_minutes")  # ya lo calcula sanitize
    if age_min is not None and not math.isnan(age_min) and age_min < MIN_AGE_MIN:
        log.debug("⏳ %s age %.1fm < %.1f → too_young", sym, age_min, MIN_AGE_MIN)
        return None  # re-queue, todavía muy pronto

    # 2) antigüedad absoluta ---------------------------------------------------------
    created = token.get("created_at")
    if isinstance(created, str):
        created = parse_iso_utc(created)
    if not created:
        log.debug("✗ %s sin created_at", sym)
        return False
    if getattr(created, "tzinfo", None) is None:
        created = created.replace(tzinfo=timezone.utc)

    age_sec = (utc_now() - created).total_seconds()
    age_days = age_sec / 86_400
    if age_days > MAX_AGE_DAYS:
        log.debug("✗ %s age %.1f d > %s", sym, age_days, MAX_AGE_DAYS)
        return False

    # 3) liquidez • volumen • market-cap --------------------------------------------
    liq = token.get("liquidity_usd")
    vol24 = token.get("volume_24h_usd")
    mcap = token.get("market_cap_usd")

    # — ajustes suaves para Pump.fun —
    is_pf = token.get("discovered_via") == "pumpfun"
    min_liq_th = MIN_LIQUIDITY_USD if not is_pf else max(1000.0, MIN_LIQUIDITY_USD * 0.6)
    max_mcap_th = MAX_MARKET_CAP_USD if not is_pf else MAX_MARKET_CAP_USD * 1.5

    # 3.a —ventana de gracia 0-5 min—
    if age_sec < 300:
        # Dentro de los 5 min no aplicamos cortes estrictos de liq/vol/mcap.
        pass
    else:
        if liq is None or (isinstance(liq, float) and math.isnan(liq)) or liq < min_liq_th:
            log.debug(
                "✗ %s liq %.0f < %.0f (umbral %s)",
                sym, liq or 0, min_liq_th,
                "PF" if is_pf else "STD",
            )
            return False

        if (
            vol24 is None
            or (isinstance(vol24, float) and math.isnan(vol24))
            or vol24 < MIN_VOL_USD_24H
            or vol24 > MAX_24H_VOLUME
        ):
            log.debug("✗ %s vol24h %.0f fuera rango (tras 5 min)", sym, vol24 or 0)
            return False

        # mcap: mantenemos el mínimo estándar y relajamos el máximo si es PF
        if (
            mcap is not None
            and not (isinstance(mcap, float) and math.isnan(mcap))
            and (mcap < MIN_MARKET_CAP_USD or mcap > max_mcap_th)
        ):
            log.debug(
                "✗ %s mcap %.0f fuera rango [%.0f-%.0f]%s",
                sym, mcap or 0, MIN_MARKET_CAP_USD, max_mcap_th, " (PF)" if is_pf else ""
            )
            return False

    # 3.b —tokens muy recientes con liq==0 → delay—
    if age_sec < 60 and (liq is None or (isinstance(liq, float) and math.isnan(liq)) or liq == 0):
        log.debug("⏳ %s liq 0/NaN con age<60 s → requeue", sym)
        return None

    # 4) holders --------------------------------------------------------------------
    holders = token.get("holders", 0) or 0
    if holders == 0:
        swaps_5m = token.get("txns_last_5m", 0) or 0
        if swaps_5m == 0:
            log.debug("✗ %s holders=0 y 0 swaps – muy temprano", sym)
            return False
    elif holders < MIN_HOLDERS:
        log.debug("✗ %s holders %s < %s", sym, holders, MIN_HOLDERS)
        return False

    # 5) early sell-off --------------------------------------------------------------
    if age_sec < 600:
        sells = token.get("txns_last_5m_sells") or 0
        buys = token.get("txns_last_5m") or 0
        if (sells + buys) and sells / (sells + buys) > 0.7:
            pc5 = (
                token.get("priceChange", {}).get("m5")
                or token.get("price_change_5m")
                or 0
            )
            try:
                pc5_val = float(pc5)
            except Exception:
                pc5_val = 0.0
            # Algunas fuentes traen 0.02 (2%) y otras 2 (2)
            if -5 < pc5_val < 5:
                # asumimos unidades en % ya; dejamos tal cual
                pass
            else:
                # si parece fracción, lo pasamos a %
                if abs(pc5_val) < 1.0:
                    pc5_val *= 100.0
            if -5 < pc5_val < 5:
                log.debug("✗ %s >70%% ventas iniciales (precio estable ±5%%)", sym)
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
