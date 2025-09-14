# memebot3/analytics/filters.py
"""
Filtro de pares basado en reglas básicas (no-IA).

Cambios
───────
2025-09-15
• (QW #1) Evitar falsos positivos: se quita "sell" de _FORBIDDEN_SUBSTR y se
  añade excepción explícita para claves que empiecen por "txns_last_5m_sell"
  (p.ej., "txns_last_5m_sells") en el aviso no-T0.
• (QW #6) Log "too_young" con más decimales y muestra también el valor raw.

2025-09-14
• Relajado el gate «holders==0 && swaps_5m==0»: ahora solo bloquea con *delay* (None)
  cuando el token es muy nuevo (age < max(1.0, 2×MIN_AGE_MIN)); ya no descarta
  definitivamente a T0 por ese motivo.
• Si holders < MIN_HOLDERS pero el token es muy nuevo, devolvemos *delay* (None) en
  lugar de descartar, para permitir validar más tarde.
• Mantiene el corte de edad mínima usando MIN_AGE_MIN del .env (no hardcode 3.0).

2025-09-13
• Revisión de coherencia T0: se ignoran/avisan claves de “futuro” si llegan en el token.
• Tratamiento explícito de NaN/None en cortes; sin cambios estructurales.
• Normalización defensiva de created_at y address.

2025-08-28
• Nuevo parámetro .env: BLOCK_HOURS (ej. "17" o "3,12,17-19") para bloquear horas
  locales específicas (además de las ventanas TRADING_HOURS/EXTRA). Si está vacío,
  no bloquea nada.

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
from datetime import datetime, timezone
from typing import Dict, Optional

import numpy as np  # noqa: F401  (mantenido por compat; puede usarse en features futuras)

from config.config import (
    MAX_24H_VOLUME,
    MAX_AGE_DAYS,
    MAX_MARKET_CAP_USD,
    MIN_AGE_MIN,
    MIN_HOLDERS,
    MIN_LIQUIDITY_USD,
    MIN_MARKET_CAP_USD,
    MIN_VOL_USD_24H,
    MIN_SCORE_TOTAL,
    LOCAL_TZ,
)
from utils.time import utc_now, is_in_trading_window, parse_iso_utc

log = logging.getLogger("filters")

# Claves sospechosas de “futuro” que no deben influir en filtros T0 (solo aviso)
# (QW #1) OJO: quitamos "sell" para no atrapar "txns_last_5m_sells".
_FORBIDDEN_SUBSTR = (
    "pnl",
    "close_price",
    "_at_close",
    "_after_",
    "outcome",
    "result",
    # "sell",  # ← removido: causaba falsos positivos con txns_last_5m_sells
    "exit",
    "tp_",
    "sl_",
)


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


def _parse_block_hours(raw: str) -> set[int]:
    """
    Acepta '17', '3,12,17-19', espacios, y devuelve un set de horas [0..23].
    Soporta rangos 'a-b' inclusivos.
    """
    out: set[int] = set()
    if not raw:
        return out
    for chunk in str(raw).split(","):
        c = chunk.strip()
        if not c:
            continue
        if "-" in c:
            a, b = [x.strip() for x in c.split("-", 1)]
            try:
                ia, ib = int(a), int(b)
            except ValueError:
                continue
            ia = max(0, min(23, ia))
            ib = max(0, min(23, ib))
            if ia > ib:
                ia, ib = ib, ia
            out.update(range(ia, ib + 1))
        else:
            try:
                h = int(c)
                if 0 <= h <= 23:
                    out.add(h)
            except ValueError:
                continue
    return out


def _warn_if_future_keys(token: Dict) -> None:
    """Log de advertencia si el token trae claves que parecen de 'futuro'."""
    bad = []
    for k in token.keys():
        lk = str(k).lower()
        if any(sub in lk for sub in _FORBIDDEN_SUBSTR):
            # Excepción concreta (QW #1): no avisar por "txns_last_5m_sells*"
            if lk.startswith("txns_last_5m_sell"):
                continue
            bad.append(k)
    if bad:
        log.debug("⚠️  token incluye claves no-T0 ignorables para filtros: %s", bad)


def _is_nan_or_none(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _to_float_or_none(x) -> Optional[float]:
    try:
        if x is None:
            return None
        # Evitar tratar strings vacíos como 0
        if isinstance(x, str) and not x.strip():
            return None
        v = float(x)
        if math.isnan(v):
            return None
        return v
    except Exception:
        return None


# ─────────────────────────── FILTRO DURO ────────────────────────────
def basic_filters(token: Dict) -> Optional[bool]:
    """
    True   → pasa el filtro duro
    False  → descartado definitivamente
    None   → “delay”: re-encolar y reintentar más tarde
    """
    _warn_if_future_keys(token)

    addr = _extract_address(token)
    sym = token.get("symbol") or (addr[:4] if addr else "????")

    # -1) gate horario condicionado por .env:
    #     Si TRADING_HOURS y TRADING_HOURS_EXTRA están vacíos → no filtrar por hora.
    #     Si alguna está definida → aplicar check con is_in_trading_window().
    H = (os.getenv("TRADING_HOURS", "") or "").strip()
    E = (os.getenv("TRADING_HOURS_EXTRA", "") or "").strip()
    if H or E:
        if not is_in_trading_window():
            log.debug("⏸ %s fuera de ventana horaria → requeue", sym)
            return None

    # -1.b) bloqueo de horas explícitas (BLOCK_HOURS, independiente de TRADING_HOURS)
    raw_block = (os.getenv("BLOCK_HOURS", "") or "").strip()
    if raw_block:
        blocked = _parse_block_hours(raw_block)
        if blocked:
            now_local = datetime.now(LOCAL_TZ)
            if now_local.hour in blocked:
                log.debug("⏸ %s hora bloqueada (%02d:00 local) → requeue", sym, now_local.hour)
                return None

    # 0) red: sólo Solana -----------------------------------------------------------
    if not _is_chain_solana(token):
        log.debug("✗ %s chainId≠solana (descartado)", sym)
        return False

    if not addr or not _is_solana_address(addr):
        log.debug("✗ %s address no-Solana/incorrecta (%r)", sym, addr)
        return False

    # 1) edad mínima ----------------------------------------------------------------
    age_min_raw = token.get("age_min") or token.get("age_minutes")
    age_min = _to_float_or_none(age_min_raw)
    if age_min is not None and age_min < float(MIN_AGE_MIN):
        # (QW #6) Más decimales y mostrar raw
        try:
            raw_disp = (
                f"{float(age_min_raw):.6f}"
                if isinstance(age_min_raw, (int, float)) or (isinstance(age_min_raw, str) and age_min_raw.strip() and _to_float_or_none(age_min_raw) is not None)
                else repr(age_min_raw)
            )
        except Exception:
            raw_disp = repr(age_min_raw)
        log.debug(
            "⏳ %s age %.3fm (raw=%s) < %.3f → too_young",
            sym,
            age_min,
            raw_disp,
            float(MIN_AGE_MIN),
        )
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

    now = utc_now()
    age_sec = (now - created).total_seconds()
    age_days = age_sec / 86_400
    if age_days > float(MAX_AGE_DAYS):
        log.debug("✗ %s age %.1f d > %s", sym, age_days, MAX_AGE_DAYS)
        return False

    # Para lógicas que necesitan minutos efectivos aunque no venga age_min:
    age_min_eff = age_min if age_min is not None else (age_sec / 60.0)
    early_barrier_min = max(1.0, float(MIN_AGE_MIN) * 2.0)

    # 3) liquidez • volumen • market-cap --------------------------------------------
    liq = _to_float_or_none(token.get("liquidity_usd"))
    vol24 = _to_float_or_none(token.get("volume_24h_usd"))
    mcap = _to_float_or_none(token.get("market_cap_usd"))

    # — ajustes suaves para Pump.fun —
    is_pf = token.get("discovered_via") == "pumpfun"
    min_liq_th = float(MIN_LIQUIDITY_USD) if not is_pf else max(1000.0, float(MIN_LIQUIDITY_USD) * 0.6)
    max_mcap_th = float(MAX_MARKET_CAP_USD) if not is_pf else float(MAX_MARKET_CAP_USD) * 1.5

    # 3.a —ventana de gracia 0-5 min—
    if age_sec < 300:
        # Dentro de los 5 min no aplicamos cortes estrictos de liq/vol/mcap.
        pass
    else:
        if _is_nan_or_none(liq) or liq < min_liq_th:
            log.debug(
                "✗ %s liq %.0f < %.0f (umbral %s)",
                sym, (0 if _is_nan_or_none(liq) else liq), min_liq_th,
                "PF" if is_pf else "STD",
            )
            return False

        if _is_nan_or_none(vol24) or vol24 < float(MIN_VOL_USD_24H) or vol24 > float(MAX_24H_VOLUME):
            log.debug("✗ %s vol24h %.0f fuera rango (tras 5 min)", sym, (0 if _is_nan_or_none(vol24) else vol24))
            return False

        # mcap: mantenemos el mínimo estándar y relajamos el máximo si es PF
        if (not _is_nan_or_none(mcap)) and (mcap < float(MIN_MARKET_CAP_USD) or mcap > max_mcap_th):
            log.debug(
                "✗ %s mcap %.0f fuera rango [%.0f-%.0f]%s",
                sym, mcap or 0, float(MIN_MARKET_CAP_USD), max_mcap_th, " (PF)" if is_pf else ""
            )
            return False

    # 3.b —tokens muy recientes con liq==0 → delay—
    if age_sec < 60 and (_is_nan_or_none(liq) or (liq or 0.0) == 0.0):
        log.debug("⏳ %s liq 0/NaN con age<60 s → requeue", sym)
        return None

    # 4) holders --------------------------------------------------------------------
    holders = int(_to_float_or_none(token.get("holders")) or 0)
    swaps_5m = int(_to_float_or_none(token.get("txns_last_5m")) or 0)

    # Nueva política: en edades muy tempranas devolvemos *delay* (None) en lugar de descartar.
    if holders == 0:
        if swaps_5m == 0 and age_min_eff < early_barrier_min:
            log.debug("⏳ %s holders=0 & swaps5m=0 con age %.1fm < %.1fm → requeue", sym, age_min_eff, early_barrier_min)
            return None
        # Dejar pasar a siguientes cortes si hay algo de actividad o ya no es tan temprano
    elif holders < int(MIN_HOLDERS):
        if age_min_eff < early_barrier_min:
            log.debug("⏳ %s holders %d < %d pero age %.1fm < %.1fm → requeue", sym, holders, int(MIN_HOLDERS), age_min_eff, early_barrier_min)
            return None
        log.debug("✗ %s holders %s < %s", sym, holders, int(MIN_HOLDERS))
        return False

    # 5) early sell-off --------------------------------------------------------------
    if age_sec < 600:
        sells = int(_to_float_or_none(token.get("txns_last_5m_sells")) or 0)
        buys = int(_to_float_or_none(token.get("txns_last_5m")) or 0)
        if (sells + buys) and sells / (sells + buys) > 0.7:
            pc5 = (
                token.get("priceChange", {}).get("m5")
                or token.get("price_change_5m")
                or 0
            )
            pc5_val = _to_float_or_none(pc5) or 0.0
            # Algunas fuentes traen 0.02 (2%) y otras 2 (2)
            if -5 < pc5_val < 5:
                pass  # ya parece porcentaje
            else:
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
    score += 15 if tok.get("liquidity_usd", 0)   >= float(MIN_LIQUIDITY_USD) * 2 else 0
    score += 20 if tok.get("volume_24h_usd", 0)  >= float(MIN_VOL_USD_24H) * 3   else 0
    score += 10 if tok.get("holders", 0)         >= int(MIN_HOLDERS) * 2       else 0
    score += 15 if tok.get("rug_score", 0)       >= 70                          else 0
    score += 15 if not tok.get("cluster_bad", 0)                                else 0
    score += 10 if tok.get("social_ok", 0)                                      else 0
    score += 10 if not tok.get("insider_sig", 0)                                else 0
    return score


# ───────────────────── helper predicciones IA ───────────────────────
def ai_pred_to_filter(pred: float) -> bool:
    """Convierte probabilidad del modelo a corte booleano (placeholder simple)."""
    return pred >= float(MIN_SCORE_TOTAL) / 100.0
