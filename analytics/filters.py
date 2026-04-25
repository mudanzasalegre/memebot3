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
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

# numpy es opcional para este módulo (Pylance puede avisar si el intérprete no lo tiene)
try:
    import numpy as np  # noqa: F401
except Exception:  # pragma: no cover
    np = None  # type: ignore

from config.config import (
    AI_THRESHOLD,
    BLOCK_HOURS,
    DEX_AI_THRESHOLD,
    DEX_BUY_SOFT_SCORE_MIN,
    DEX_MAX_MARKET_CAP_USD,
    DEX_MIN_AGE_MIN,
    DEX_MIN_HOLDERS,
    DEX_MIN_LIQUIDITY_USD,
    DEX_MIN_MARKET_CAP_USD,
    DEX_MIN_VOL_USD_24H,
    DEX_REQUIRE_JUPITER_FOR_BUY,
    FILTER_PROFILE_BY_DISCOVERY,
    LOCAL_TZ,
    MAX_24H_VOLUME,
    MAX_AGE_DAYS,
    MAX_MARKET_CAP_USD,
    MIN_AGE_MIN,
    MIN_HOLDERS,
    MIN_LIQUIDITY_USD,
    MIN_MARKET_CAP_USD,
    MIN_SCORE_TOTAL,
    MIN_VOL_USD_24H,
    PUMPFUN_AI_THRESHOLD,
    PUMPFUN_BUY_SOFT_SCORE_MIN,
    PUMPFUN_MAX_MARKET_CAP_USD,
    PUMPFUN_MIN_AGE_MIN,
    PUMPFUN_MIN_HOLDERS,
    PUMPFUN_MIN_LIQUIDITY_USD,
    PUMPFUN_MIN_MARKET_CAP_USD,
    PUMPFUN_MIN_VOL_USD_24H,
    PUMPFUN_REQUIRE_JUPITER_FOR_BUY,
    REVIVAL_AI_THRESHOLD,
    REVIVAL_BUY_SOFT_SCORE_MIN,
    REVIVAL_MAX_MARKET_CAP_USD,
    REVIVAL_MIN_AGE_MIN,
    REVIVAL_MIN_HOLDERS,
    REVIVAL_MIN_LIQUIDITY_USD,
    REVIVAL_MIN_MARKET_CAP_USD,
    REVIVAL_MIN_VOL_USD_24H,
    REVIVAL_REQUIRE_JUPITER_FOR_BUY,
    SNAPSHOT_ALLOWED_PRICE_SOURCES,
    SNAPSHOT_MAX_MISSING_FIELDS,
    SNAPSHOT_QUALITY_FILTER_ENABLED,
    SNAPSHOT_REQUIRE_ACTIVITY_SIGNAL,
    SNAPSHOT_REQUIRE_RUG_SCORE,
    SNAPSHOT_REQUIRE_SOCIAL_OR_TREND,
    TRADING_HOURS,
    TRADING_HOURS_EXTRA,
)
from utils.time import is_in_trading_window, parse_iso_utc, utc_now

log = logging.getLogger("filters")


@dataclass(frozen=True)
class FilterThresholds:
    regime: str
    min_age_min: float
    min_holders: int
    min_liquidity_usd: float
    min_vol_usd_24h: float
    min_market_cap_usd: float
    max_market_cap_usd: float


_REGIME_THRESHOLD_OVERRIDES: dict[str, dict[str, float | int | None]] = {
    "dex": {
        "min_age_min": DEX_MIN_AGE_MIN,
        "min_holders": DEX_MIN_HOLDERS,
        "min_liquidity_usd": DEX_MIN_LIQUIDITY_USD,
        "min_vol_usd_24h": DEX_MIN_VOL_USD_24H,
        "min_market_cap_usd": DEX_MIN_MARKET_CAP_USD,
        "max_market_cap_usd": DEX_MAX_MARKET_CAP_USD,
    },
    "pumpfun": {
        "min_age_min": PUMPFUN_MIN_AGE_MIN,
        "min_holders": PUMPFUN_MIN_HOLDERS,
        "min_liquidity_usd": PUMPFUN_MIN_LIQUIDITY_USD,
        "min_vol_usd_24h": PUMPFUN_MIN_VOL_USD_24H,
        "min_market_cap_usd": PUMPFUN_MIN_MARKET_CAP_USD,
        "max_market_cap_usd": PUMPFUN_MAX_MARKET_CAP_USD,
    },
    "revival": {
        "min_age_min": REVIVAL_MIN_AGE_MIN,
        "min_holders": REVIVAL_MIN_HOLDERS,
        "min_liquidity_usd": REVIVAL_MIN_LIQUIDITY_USD,
        "min_vol_usd_24h": REVIVAL_MIN_VOL_USD_24H,
        "min_market_cap_usd": REVIVAL_MIN_MARKET_CAP_USD,
        "max_market_cap_usd": REVIVAL_MAX_MARKET_CAP_USD,
    },
}

_REGIME_SOFT_SCORE_MIN: dict[str, int | None] = {
    "dex": DEX_BUY_SOFT_SCORE_MIN,
    "pumpfun": PUMPFUN_BUY_SOFT_SCORE_MIN,
    "revival": REVIVAL_BUY_SOFT_SCORE_MIN,
}

_REGIME_AI_THRESHOLD: dict[str, float | None] = {
    "dex": DEX_AI_THRESHOLD,
    "pumpfun": PUMPFUN_AI_THRESHOLD,
    "revival": REVIVAL_AI_THRESHOLD,
}

_REGIME_REQUIRE_JUP: dict[str, bool | None] = {
    "dex": DEX_REQUIRE_JUPITER_FOR_BUY,
    "pumpfun": PUMPFUN_REQUIRE_JUPITER_FOR_BUY,
    "revival": REVIVAL_REQUIRE_JUPITER_FOR_BUY,
}


def _normalize_regime(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"pump_early", "pumpfun", "pump", "pump_fun"}:
        return "pumpfun"
    if raw in {"revival", "revive", "revived"}:
        return "revival"
    return "dex"


def _resolve_profile_regime(token: dict[str, Any]) -> str:
    entry_regime = str(token.get("entry_regime") or "").strip()
    if entry_regime:
        return _normalize_regime(entry_regime)
    return _normalize_regime(token.get("discovered_via"))


def _threshold_value(regime: str, key: str, default: float | int) -> float | int:
    if not FILTER_PROFILE_BY_DISCOVERY:
        return default
    override = _REGIME_THRESHOLD_OVERRIDES.get(regime, {}).get(key)
    return default if override is None else override


def effective_thresholds(token: dict[str, Any]) -> FilterThresholds:
    regime = _resolve_profile_regime(token)
    return FilterThresholds(
        regime=regime,
        min_age_min=float(_threshold_value(regime, "min_age_min", MIN_AGE_MIN)),
        min_holders=int(_threshold_value(regime, "min_holders", MIN_HOLDERS)),
        min_liquidity_usd=float(_threshold_value(regime, "min_liquidity_usd", MIN_LIQUIDITY_USD)),
        min_vol_usd_24h=float(_threshold_value(regime, "min_vol_usd_24h", MIN_VOL_USD_24H)),
        min_market_cap_usd=float(_threshold_value(regime, "min_market_cap_usd", MIN_MARKET_CAP_USD)),
        max_market_cap_usd=float(_threshold_value(regime, "max_market_cap_usd", MAX_MARKET_CAP_USD)),
    )


def effective_soft_score_min(token: dict[str, Any], default_value: int) -> int:
    regime = _resolve_profile_regime(token)
    if not FILTER_PROFILE_BY_DISCOVERY:
        return int(default_value)
    override = _REGIME_SOFT_SCORE_MIN.get(regime)
    return int(default_value if override is None else override)


def effective_ai_threshold(token: dict[str, Any], default_value: float) -> float:
    regime = _resolve_profile_regime(token)
    if not FILTER_PROFILE_BY_DISCOVERY:
        return float(default_value)
    override = _REGIME_AI_THRESHOLD.get(regime)
    return float(default_value if override is None else override)


def effective_require_jupiter_for_buy(token: dict[str, Any], default_value: bool) -> bool:
    regime = _resolve_profile_regime(token)
    if not FILTER_PROFILE_BY_DISCOVERY:
        return bool(default_value)
    override = _REGIME_REQUIRE_JUP.get(regime)
    return bool(default_value if override is None else override)


def _trend_positive(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"up", "uptrend", "bull", "bullish", "1"}
    try:
        return float(value) > 0
    except Exception:
        return False


def snapshot_quality_gate(token: dict[str, Any]) -> tuple[bool, str | None]:
    if not SNAPSHOT_QUALITY_FILTER_ENABLED:
        return True, None

    missing = 0
    checks = {
        "liquidity_usd": token.get("liquidity_usd"),
        "volume_24h_usd": token.get("volume_24h_usd"),
        "holders": token.get("holders"),
        "social_ok": token.get("social_ok"),
        "trend": token.get("trend"),
    }
    if SNAPSHOT_REQUIRE_RUG_SCORE:
        checks["rug_score"] = token.get("rug_score")

    for key, value in checks.items():
        if key in {"social_ok", "trend"}:
            if value is None:
                missing += 1
        else:
            fv = _to_float_or_none(value)
            if fv is None or fv <= 0:
                missing += 1

    if missing > int(SNAPSHOT_MAX_MISSING_FIELDS):
        return False, f"missing_fields>{SNAPSHOT_MAX_MISSING_FIELDS}"

    if SNAPSHOT_REQUIRE_ACTIVITY_SIGNAL:
        holders = int(_to_float_or_none(token.get("holders")) or 0)
        txns_5m = int(_to_float_or_none(token.get("txns_last_5m")) or 0)
        if holders <= 0 and txns_5m <= 0:
            return False, "missing_activity_signal"

    if SNAPSHOT_REQUIRE_SOCIAL_OR_TREND:
        social_ok = bool(token.get("social_ok") is True or token.get("social_ok") == 1)
        if not social_ok and not _trend_positive(token.get("trend")):
            return False, "missing_social_or_trend"

    if SNAPSHOT_ALLOWED_PRICE_SOURCES:
        source = str(token.get("price_source") or "").strip().lower()
        if source and source not in SNAPSHOT_ALLOWED_PRICE_SOURCES:
            return False, f"price_source={source}"

    return True, None


def describe_filter_policy() -> dict[str, Any]:
    return {
        "profile_by_discovery": bool(FILTER_PROFILE_BY_DISCOVERY),
        "snapshot_quality_filter_enabled": bool(SNAPSHOT_QUALITY_FILTER_ENABLED),
        "snapshot_max_missing_fields": int(SNAPSHOT_MAX_MISSING_FIELDS),
        "snapshot_require_activity_signal": bool(SNAPSHOT_REQUIRE_ACTIVITY_SIGNAL),
        "snapshot_require_social_or_trend": bool(SNAPSHOT_REQUIRE_SOCIAL_OR_TREND),
        "snapshot_require_rug_score": bool(SNAPSHOT_REQUIRE_RUG_SCORE),
        "snapshot_allowed_price_sources": ",".join(SNAPSHOT_ALLOWED_PRICE_SOURCES) or "(any)",
        "dex_overrides_active": any(v is not None for v in _REGIME_THRESHOLD_OVERRIDES["dex"].values()) or any(
            v is not None for v in (DEX_BUY_SOFT_SCORE_MIN, DEX_AI_THRESHOLD, DEX_REQUIRE_JUPITER_FOR_BUY)
        ),
        "pumpfun_overrides_active": any(v is not None for v in _REGIME_THRESHOLD_OVERRIDES["pumpfun"].values()) or any(
            v is not None for v in (PUMPFUN_BUY_SOFT_SCORE_MIN, PUMPFUN_AI_THRESHOLD, PUMPFUN_REQUIRE_JUPITER_FOR_BUY)
        ),
        "revival_overrides_active": any(v is not None for v in _REGIME_THRESHOLD_OVERRIDES["revival"].values()) or any(
            v is not None for v in (REVIVAL_BUY_SOFT_SCORE_MIN, REVIVAL_AI_THRESHOLD, REVIVAL_REQUIRE_JUPITER_FOR_BUY)
        ),
    }

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
def _extract_address(token: dict[str, Any]) -> str | None:
    """Extrae el mint address desde varias claves posibles."""
    addr = (
        token.get("address")
        or token.get("token_address")
        or token.get("tokenAddress")
        or token.get("mint")
        or token.get("baseMint")
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


def _is_chain_solana(token: dict[str, Any]) -> bool:
    """
    Si viene chainId/chain, debe ser 'solana' (o 'sol').
    Si no viene, no bloqueamos por compatibilidad con distintas fuentes.
    """
    cid = token.get("chainId") or token.get("chain") or token.get("chainIdShort")
    if cid is None:
        return True
    cid = str(cid).lower().strip()
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
            except ValueError:
                continue
            if 0 <= h <= 23:
                out.add(h)

    return out


def _warn_if_future_keys(token: dict[str, Any]) -> None:
    """Log de advertencia si el token trae claves que parecen de 'futuro'."""
    bad: list[str] = []
    for k in token.keys():
        lk = str(k).lower()
        if any(sub in lk for sub in _FORBIDDEN_SUBSTR):
            # Excepción concreta (QW #1): no avisar por "txns_last_5m_sells*"
            if lk.startswith("txns_last_5m_sell"):
                continue
            bad.append(str(k))
    if bad:
        log.debug("⚠️  token incluye claves no-T0 ignorables para filtros: %s", bad)


def _is_nan_or_none(x: Any) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _to_float_or_none(x: Any) -> Optional[float]:
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


def _sym_for_log(token: dict[str, Any], addr: Optional[str]) -> str:
    s = token.get("symbol") or token.get("baseTokenSymbol") or token.get("ticker")
    if isinstance(s, str) and s.strip():
        return s.strip()
    return (addr[:4] if addr else "????")


def _extract_created_at(token: dict[str, Any]) -> Optional[datetime]:
    """
    Intenta obtener created_at de distintas fuentes habituales.
    Devuelve datetime timezone-aware (UTC) o None.
    """
    created = token.get("created_at") or token.get("createdAt") or token.get("created") or token.get("createdAtUtc")
    if isinstance(created, datetime):
        dt = created
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    if isinstance(created, str) and created.strip():
        dt = parse_iso_utc(created.strip())
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

    # DexScreener típico: pairCreatedAt en ms
    pc = token.get("pairCreatedAt") or token.get("pair_created_at") or token.get("pairCreatedAtMs")
    pc_f = _to_float_or_none(pc)
    if pc_f:
        try:
            # Si parece ms (muy grande), convertir
            ts = pc_f / 1000.0 if pc_f > 1e11 else pc_f
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt
        except Exception:
            return None

    return None


# ─────────────────────────── FILTRO DURO ────────────────────────────
def basic_filters(token: dict[str, Any]) -> Optional[bool]:
    """
    True   → pasa el filtro duro
    False  → descartado definitivamente
    None   → “delay”: re-encolar y reintentar más tarde
    """
    if not isinstance(token, dict):
        return False

    _warn_if_future_keys(token)

    addr = _extract_address(token)
    sym = _sym_for_log(token, addr)
    thresholds = effective_thresholds(token)

    # -1) gate horario condicionado por .env:
    #     Si TRADING_HOURS y TRADING_HOURS_EXTRA están vacíos → no filtrar por hora.
    #     Si alguna está definida → aplicar check con is_in_trading_window().
    if (TRADING_HOURS or "").strip() or (TRADING_HOURS_EXTRA or "").strip():
        if not is_in_trading_window():
            log.debug("⏸ %s fuera de ventana horaria → requeue", sym)
            return None

    # -1.b) bloqueo de horas explícitas (BLOCK_HOURS, independiente de TRADING_HOURS)
    raw_block = (BLOCK_HOURS or "").strip()
    if raw_block:
        blocked = _parse_block_hours(raw_block)
        if blocked:
            try:
                now_local = datetime.now(LOCAL_TZ)
            except Exception:
                # Fallback: hora local naive (sistema)
                now_local = datetime.now()
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
    # Priorizamos created_at si existe; age_min/age_minutes queda como fallback.
    age_min_raw = token.get("age_min") or token.get("age_minutes")
    age_min = _to_float_or_none(age_min_raw)

    created = _extract_created_at(token)
    if created is None and age_min is not None:
        created = utc_now() - timedelta(minutes=float(age_min))
    if created is None:
        log.debug("✗ %s sin created_at ni age_min", sym)
        return False

    now = utc_now()
    age_sec = (now - created).total_seconds()
    if age_sec < 0:
        log.debug("⏳ %s created_at en el futuro (age_sec=%.1f) → requeue", sym, age_sec)
        return None

    age_days = age_sec / 86_400.0
    if age_days > float(MAX_AGE_DAYS):
        log.debug("✗ %s age %.2f d > %s", sym, age_days, MAX_AGE_DAYS)
        return False

    age_min_eff = age_sec / 60.0

    # Corte "too-young": requeue si < MIN_AGE_MIN
    if age_min_eff < float(thresholds.min_age_min):
        # (QW #6) Más decimales y mostrar raw si existía
        raw_disp = repr(age_min_raw)
        try:
            if age_min_raw is not None:
                v = _to_float_or_none(age_min_raw)
                if v is not None:
                    raw_disp = f"{v:.6f}"
        except Exception:
            pass

        log.debug(
            "⏳ %s age %.6fm (raw=%s) < %.6f → too_young",
            sym,
            float(age_min_eff),
            raw_disp,
            float(thresholds.min_age_min),
        )
        return None

    # Barrera temprana (para “suavizar” holders=0, etc.)
    early_barrier_min = max(1.0, float(thresholds.min_age_min) * 2.0)

    # 3) liquidez • volumen • market-cap --------------------------------------------
    liq = _to_float_or_none(token.get("liquidity_usd") or (token.get("liquidity") or {}).get("usd"))
    vol24 = _to_float_or_none(token.get("volume_24h_usd") or token.get("volume24hUsd") or (token.get("volume") or {}).get("h24"))
    mcap = _to_float_or_none(token.get("market_cap_usd") or token.get("fdv") or token.get("marketCapUsd"))

    # — ajustes suaves para Pump.fun —
    is_pf = thresholds.regime == "pumpfun"
    min_liq_th = float(thresholds.min_liquidity_usd) if not is_pf else max(1000.0, float(thresholds.min_liquidity_usd) * 0.6)
    max_mcap_th = float(thresholds.max_market_cap_usd) if not is_pf else float(thresholds.max_market_cap_usd) * 1.5

    # 3.a —ventana de gracia 0-5 min—
    if age_sec >= 300:
        if _is_nan_or_none(liq) or float(liq) < float(min_liq_th):
            log.debug(
                "✗ %s liq %.0f < %.0f (umbral %s)",
                sym,
                (0.0 if _is_nan_or_none(liq) else float(liq)),
                float(min_liq_th),
                "PF" if is_pf else "STD",
            )
            return False

        if _is_nan_or_none(vol24) or float(vol24) < float(thresholds.min_vol_usd_24h) or float(vol24) > float(MAX_24H_VOLUME):
            log.debug(
                "✗ %s vol24h %.0f fuera rango [%.0f-%.0f] (tras 5 min)",
                sym,
                (0.0 if _is_nan_or_none(vol24) else float(vol24)),
                float(thresholds.min_vol_usd_24h),
                float(MAX_24H_VOLUME),
            )
            return False

        # mcap: mantenemos el mínimo estándar y relajamos el máximo si es PF
        if (not _is_nan_or_none(mcap)) and (
            float(mcap) < float(thresholds.min_market_cap_usd) or float(mcap) > float(max_mcap_th)
        ):
            log.debug(
                "✗ %s mcap %.0f fuera rango [%.0f-%.0f]%s",
                sym,
                float(mcap) if mcap is not None else 0.0,
                float(thresholds.min_market_cap_usd),
                float(max_mcap_th),
                " (PF)" if is_pf else "",
            )
            return False

    # 3.b —tokens ultra recientes con liq==0 → delay—
    if age_sec < 60 and (_is_nan_or_none(liq) or float(liq or 0.0) == 0.0):
        log.debug("⏳ %s liq 0/NaN con age<60 s → requeue", sym)
        return None

    # 4) holders / swaps -------------------------------------------------------------
    holders = int(_to_float_or_none(token.get("holders")) or 0)

    swaps_5m = (
        _to_float_or_none(token.get("txns_last_5m"))
        or _to_float_or_none((token.get("txns") or {}).get("m5"))
        or _to_float_or_none(token.get("swaps_5m"))
        or 0.0
    )
    try:
        swaps_5m_i = int(swaps_5m)
    except Exception:
        swaps_5m_i = 0

    # Política (2025-09-14): en edades muy tempranas devolvemos delay en lugar de descartar.
    if holders == 0:
        if swaps_5m_i == 0 and age_min_eff < early_barrier_min:
            log.debug(
                "⏳ %s holders=0 & swaps5m=0 con age %.2fm < %.2fm → requeue",
                sym,
                float(age_min_eff),
                float(early_barrier_min),
            )
            return None
        # si no, dejamos pasar (hay actividad o ya no es tan temprano)
    elif holders < int(thresholds.min_holders):
        if age_min_eff < early_barrier_min:
            log.debug(
                "⏳ %s holders %d < %d pero age %.2fm < %.2fm → requeue",
                sym,
                holders,
                int(thresholds.min_holders),
                float(age_min_eff),
                float(early_barrier_min),
            )
            return None
        log.debug("✗ %s holders %d < %d", sym, holders, int(thresholds.min_holders))
        return False

    # 5) early sell-off --------------------------------------------------------------
    # OJO: Aquí NO miramos “sell” como substring genérico (QW#1),
    # solo la métrica concreta txns_last_5m_sells.
    if age_sec < 600:
        sells = int(_to_float_or_none(token.get("txns_last_5m_sells")) or 0)

        # buys aproximado: si la fuente da txns_last_5m como total,
        # separamos con sells; si no, asumimos buys == txns_last_5m (legacy).
        total_5m = int(_to_float_or_none(token.get("txns_last_5m")) or 0)
        buys = max(0, total_5m - sells) if total_5m else int(_to_float_or_none(token.get("txns_last_5m")) or 0)

        denom = sells + buys
        if denom > 0 and (sells / denom) > 0.7:
            pc5 = None
            price_change = token.get("priceChange")
            if isinstance(price_change, dict):
                pc5 = price_change.get("m5")
            if pc5 is None:
                pc5 = token.get("price_change_5m")

            pc5_val = _to_float_or_none(pc5) or 0.0

            # Normalización suave: si parece fracción, a %
            if abs(pc5_val) < 1.0 and pc5_val != 0.0:
                pc5_val *= 100.0

            # Si el precio apenas se movió (±5%) pero hay 70% ventas, mala señal
            if -5.0 < pc5_val < 5.0:
                log.debug("✗ %s >70%% ventas iniciales (precio estable ±5%%)", sym)
                return False

    return True


# ───────────────────────── PUNTUACIÓN SUAVE ─────────────────────────
def total_score(tok: dict[str, Any]) -> int:
    """
    Suma de puntos ‘blandos’; se usa tras pasar el filtro duro.
    Mantiene heurística simple y robusta a tipos.
    """
    if not isinstance(tok, dict):
        return 0

    def f(x: Any) -> float:
        v = _to_float_or_none(x)
        return float(v) if v is not None else 0.0

    def i(x: Any) -> int:
        try:
            return int(f(x))
        except Exception:
            return 0

    thresholds = effective_thresholds(tok)
    score = 0
    score += 15 if f(tok.get("liquidity_usd")) >= float(thresholds.min_liquidity_usd) * 2 else 0
    score += 20 if f(tok.get("volume_24h_usd")) >= float(thresholds.min_vol_usd_24h) * 3 else 0
    score += 10 if i(tok.get("holders")) >= int(thresholds.min_holders) * 2 else 0
    score += 15 if f(tok.get("rug_score")) >= 70 else 0
    score += 15 if not bool(tok.get("cluster_bad", 0)) else 0
    score += 10 if bool(tok.get("social_ok", 0)) else 0
    score += 10 if not bool(tok.get("insider_sig", 0)) else 0
    return int(score)


# ───────────────────── helper predicciones IA ───────────────────────
def ai_pred_to_filter(pred: float) -> bool:
    """
    Convierte probabilidad del modelo a corte booleano.

    Convención:
      - pred ∈ [0,1] (probabilidad)
      - AI_THRESHOLD (cfg) es el umbral principal.
      - Fallback legacy: MIN_SCORE_TOTAL/100 (si AI_THRESHOLD no es usable).
    """
    try:
        p = float(pred)
        if math.isnan(p):
            return False
    except Exception:
        return False

    # Clamp defensivo
    if p < 0.0:
        p = 0.0
    if p > 1.0:
        p = 1.0

    # Umbral principal
    th = _to_float_or_none(AI_THRESHOLD)
    if th is None:
        # Legacy fallback
        try:
            th = float(MIN_SCORE_TOTAL) / 100.0
        except Exception:
            th = 0.0

    # Clamp del umbral
    if th < 0.0:
        th = 0.0
    if th > 1.0:
        th = 1.0

    return p >= th


__all__ = [
    "FilterThresholds",
    "basic_filters",
    "total_score",
    "ai_pred_to_filter",
    "effective_thresholds",
    "effective_soft_score_min",
    "effective_ai_threshold",
    "effective_require_jupiter_for_buy",
    "snapshot_quality_gate",
    "describe_filter_policy",
]
