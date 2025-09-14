# memebot3/analytics/insider.py
"""
Detección simplificada de actividad de insiders (heurístico) en T0.

Regla (proxy):
  Dentro de los primeros WINDOW_MINUTES minutos tras el mint,
  dispara alerta si:
    • hay al menos MIN_BUYS_5M compras en los últimos 5 min, y
    • el precio sube ≥ MIN_PCT_UP_5M en 5 min, y
    • la liquidez es al menos MIN_LIQ_USD (para evitar ruido de pools ínfimos).

Notas:
  - Solo usa señales disponibles en T0 (snapshot en tiempo real).
  - No inspeccionamos tamaños de cada compra (la API pública no expone el tape completo).
  - Usamos `txns_last_5m` (buys) y `priceChange.m5` de DexScreener cuando está.
  - Si faltan datos críticos, devolvemos False (no alerta).
  - Se añaden avisos (log DEBUG) si aparecen claves que parecen de “futuro”.

Cambios
───────
2025-09-15
• Anti falsos positivos: se quita "sell" de _FORBIDDEN_SUBSTR y se añade
  excepción explícita para claves que empiecen por "txns_last_5m_sell".
  Ya no avisa por "txns_last_5m_sells".
"""

from __future__ import annotations

import logging
from typing import Final, Optional, Dict

from fetcher import dexscreener
from utils.time import utc_now, parse_iso_utc

log = logging.getLogger("insider")

# Ventana de observación tras el mint (T0)
WINDOW_MINUTES: Final[int] = 20

# Umbrales del heurístico
MIN_BUYS_5M:     Final[int]   = 3      # mínimo de compras en 5m
MIN_PCT_UP_5M:   Final[float] = 5.0    # % mínimo de subida en 5m
MIN_LIQ_USD:     Final[float] = 3000.0 # liquidez mínima para considerar la señal

# Claves sospechosas de “futuro” que NO deben influir en T0 (solo aviso)
# OJO: "sell" eliminado para no atrapar "txns_last_5m_sells".
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


def _to_float(x) -> Optional[float]:
    try:
        v = float(x)
        if v != v:  # NaN
            return None
        return v
    except Exception:
        return None


def _warn_if_future_keys(token: Dict) -> None:
    """Log de advertencia si el snapshot incluye claves que parecen de 'futuro'."""
    bad = []
    for k in token.keys():
        lk = str(k).lower()
        if any(sub in lk for sub in _FORBIDDEN_SUBSTR):
            # Excepción concreta: no avisar por "txns_last_5m_sell*"
            if lk.startswith("txns_last_5m_sell"):
                continue
            bad.append(k)
    if bad:
        log.debug("⚠️  insider: snapshot incluye claves no-T0 ignorables: %s", bad)


async def insider_alert(address: str) -> bool:
    # 1) Snapshot DexScreener normalizado (T0)
    tok = await dexscreener.get_pair(address)
    if not tok:
        return False

    _warn_if_future_keys(tok)

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
