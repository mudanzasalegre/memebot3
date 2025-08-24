# memebot3/utils/time.py
"""
Utilidades de tiempo.

Funciones clave
───────────────
utc_now()                       → datetime timezone-aware en UTC.
local_now(tz_name: str|None)    → datetime aware en la zona local (o tz_name).
to_utc(dt)                      → convierte cualquier datetime a UTC (aware).
to_local(dt, tz_name: str|None) → convierte a hora local (o tz_name).
parse_iso_utc(s)                → parsea ISO-8601 seguro y devuelve UTC aware o None.

Ventanas horarias de trading
────────────────────────────
is_in_trading_window(dt=None, windows=None)   → True si la hora local está dentro
                                               de las ventanas configuradas.
next_window_start(dt=None, windows=None)      → datetime local del próximo inicio
                                               de ventana (hoy o mañana).
seconds_until_next_window(dt=None, windows=None) → segundos hasta el próximo inicio.

Notas
─────
• Si `windows` es None, se usan las definidas en config.config.TRADING_WINDOWS_PARSED.
• Si no hay ventanas (tupla vacía), se interpreta como “sin restricción”.

Ejemplos de parse_iso_utc
─────────────────────────
>>> parse_iso_utc("2025-08-23T12:34:56Z").tzinfo is not None
True
>>> parse_iso_utc("2025-08-23T12:34:56+02:00").utcoffset().total_seconds()
0.0
>>> parse_iso_utc(None) is None
True
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Tuple

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# ──────────────────────── básicos UTC / local ─────────────────────────
def utc_now() -> datetime:
    """Shorthand para `datetime.now(timezone.utc)` (aware)."""
    return datetime.now(timezone.utc)


def local_now(tz_name: Optional[str] = None) -> datetime:
    """
    Devuelve el *ahora* en zona local (aware). Si `tz_name` se proporciona
    y está disponible (IANA, p.ej. 'Europe/Madrid'), se usa esa zona.
    """
    if tz_name and ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    # Fallback: zona local del sistema
    return datetime.now().astimezone()


def to_utc(dt: datetime) -> datetime:
    """
    Convierte `dt` a UTC (aware). Si `dt` es naïve, se asume hora local.
    """
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpreta naïve como local
    return dt.astimezone(timezone.utc)


def to_local(dt: datetime, tz_name: Optional[str] = None) -> datetime:
    """
    Convierte `dt` a zona local o a la especificada por `tz_name`.
    Si `dt` es naïve, se asume que está en zona local actual.
    """
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpreta naïve como local
    if tz_name and ZoneInfo is not None:
        try:
            return dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass
    return dt.astimezone()  # zona local del sistema


# ──────────────────────── parseo robusto de ISO-8601 ────────────────────────
def parse_iso_utc(s: Optional[str]) -> Optional[datetime]:
    """
    Parsea una cadena ISO-8601 y devuelve un datetime *aware en UTC*.
    Devuelve None si `s` es falsy o no se puede parsear.

    Reglas:
      • Acepta sufijo 'Z' → se trata como UTC.
      • Si viene con offset (+HH:MM), se normaliza a UTC.
      • Si es naïve (sin tzinfo), se asume UTC (evita `.replace` sobre None).
    """
    if not s:
        return None
    try:
        # Normaliza 'Z' a '+00:00' si aparece
        txt = s.strip()
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            # Muchos proveedores devuelven ISO sin tz pero en UTC
            dt = dt.replace(tzinfo=timezone.utc)
        # Devuelve ya en UTC
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ─────────────────────── ventanas de trading ───────────────────────────
def _get_windows_default() -> Tuple[Tuple[int, int], ...]:
    """
    Obtiene ventanas por defecto desde config.config.TRADING_WINDOWS_PARSED.
    Import diferido para evitar ciclos de importación.
    """
    try:
        from config.config import TRADING_WINDOWS_PARSED  # type: ignore
        return tuple(TRADING_WINDOWS_PARSED)
    except Exception:
        return tuple()


def _hour_in_windows(hour: int, windows: Iterable[Tuple[int, int]]) -> bool:
    """Devuelve True si `hour` (0–23) está dentro de alguna ventana (s,e) inclusiva."""
    h = max(0, min(23, int(hour)))
    for start, end in windows:
        if start <= h <= end:
            return True
    return False


def is_in_trading_window(
    dt: Optional[datetime] = None,
    windows: Optional[Tuple[Tuple[int, int], ...]] = None,
) -> bool:
    """
    True si la hora local de `dt` está dentro de las ventanas de trading.
    Si `windows` es None, usa las definidas en config. Si no hay ventanas,
    se interpreta como “sin restricción” (siempre True).
    """
    if windows is None:
        windows = _get_windows_default()
    if not windows:
        return True  # sin restricción

    local_dt = to_local(dt or datetime.now())
    return _hour_in_windows(local_dt.hour, windows)


def next_window_start(
    dt: Optional[datetime] = None,
    windows: Optional[Tuple[Tuple[int, int], ...]] = None,
) -> Optional[datetime]:
    """
    Devuelve el datetime (local) del próximo inicio de ventana >= ahora.
    Si ya estás dentro de una ventana, devuelve el inicio de la ventana actual
    (hora redondeada a :00). Si no hay ventanas, devuelve None.
    """
    if windows is None:
        windows = _get_windows_default()
    if not windows:
        return None

    now_local = to_local(dt or datetime.now())
    today = now_local.date()

    # Genera candidatos (inicio de ventana) para hoy y mañana
    starts = []
    for day_offset in (0, 1):
        base_date = today + timedelta(days=day_offset)
        for start, _ in windows:
            starts.append(datetime(
                base_date.year, base_date.month, base_date.day,
                start, 0, 0, tzinfo=now_local.tzinfo
            ))

    # Si estamos dentro de una ventana, el "próximo inicio" es el inicio de esa misma
    if is_in_trading_window(now_local, windows):
        # Busca la ventana actual
        for start, end in windows:
            if start <= now_local.hour <= end:
                cur_start = datetime(
                    today.year, today.month, today.day, start, 0, 0, tzinfo=now_local.tzinfo
                )
                return cur_start

    # Si estamos fuera, devuelve el primer inicio >= ahora (sino, el de mañana)
    for s in sorted(starts):
        if s >= now_local:
            return s
    # fallback teórico (no debería ocurrir)
    return sorted(starts)[0]


def seconds_until_next_window(
    dt: Optional[datetime] = None,
    windows: Optional[Tuple[Tuple[int, int], ...]] = None,
) -> Optional[int]:
    """
    Segundos hasta el próximo inicio de ventana (0 si ya estás dentro).
    Si no hay ventanas, devuelve None.
    """
    start = next_window_start(dt=dt, windows=windows)
    if start is None:
        return None
    now_local = to_local(dt or datetime.now())
    delta = (start - now_local).total_seconds()
    return int(max(0, round(delta)))
