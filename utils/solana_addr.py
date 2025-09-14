# utils/solana_addr.py
from __future__ import annotations

import logging
from typing import Optional, Dict

log = logging.getLogger("solana_addr")

# Rango típico de longitud Base58 de mints SPL en Solana
_MIN_LEN = 30
_MAX_LEN = 50

# Cache para evitar spamear logs con el mismo "strip 'pump'"
# Clave: addr ya .strip()'eada; Valor: versión limpiada (con o sin 'pump').
_PUMP_STRIP_CACHE: Dict[str, str] = {}

# Base58 (opcional, pero recomendado para validar 32 bytes)
try:
    from base58 import b58decode as _b58decode  # pip install base58
except Exception:  # pragma: no cover - entorno sin dependencia
    _b58decode = None  # type: ignore[attr-defined]
    _BASE58_IMPORT_OK = False
else:  # pragma: no cover
    _BASE58_IMPORT_OK = True


def is_probably_mint(s: str | None) -> bool:
    """
    Heurística ligera: parece un mint SPL válido (no 0x, longitud 30–50).

    Nota: Esto NO garantiza que sea un mint válido de 32 bytes.
    Para eso usa la validación Base58 de 32 bytes (is_valid_base58_32).
    """
    if not s:
        return False
    s = s.strip()
    if not s or s.startswith("0x"):
        return False
    return _MIN_LEN <= len(s) <= _MAX_LEN


def _strip_pump_suffix(addr: str) -> str:
    """Quita un sufijo literal 'pump' (muy común en feeds de Pump.fun).

    Usa un pequeño caché para:
      • Evitar repetir el log para el mismo mint.
      • Devolver inmediatamente la versión ya normalizada.
    """
    s = addr.strip()

    # Si ya lo procesamos, devolvemos el valor cacheado (sin re-log).
    cached = _PUMP_STRIP_CACHE.get(s)
    if cached is not None:
        return cached

    # Sólo si el sufijo es exactamente 'pump' (lowercase) y está al final.
    if s.endswith("pump"):
        cleaned = s[:-4]
        try:
            log.debug("[addr] strip 'pump': %s → %s", s, cleaned)
        except Exception:
            # El log no es crítico; seguimos.
            pass
        _PUMP_STRIP_CACHE[s] = cleaned
        return cleaned

    # Si no hay 'pump', no almacenamos para no crecer de forma innecesaria.
    return s


def is_valid_base58_32(s: str) -> bool:
    """
    Valida que `s`:
      1) Sea Base58 válido.
      2) Decodifique exactamente a 32 bytes (clave pública SPL).
    """
    if not _BASE58_IMPORT_OK:
        # Sin la lib base58, no podemos garantizar validez estricta.
        # Caemos a la heurística de longitud como mejor esfuerzo.
        return is_probably_mint(s)

    try:
        decoded = _b58decode(s)  # type: ignore[misc]
    except Exception:
        return False
    return len(decoded) == 32


def normalize_mint(addr: str | None) -> Optional[str]:
    """
    Devuelve un mint normalizado o None si no parece un mint SPL.

    Proceso:
      - trim
      - quita sufijo 'pump' si está presente (con caché anti-ruido de logs)
      - valida heurísticamente longitud y no-0x
      - valida que la codificación Base58 decodifique a 32 bytes (si hay lib base58)
    """
    if not addr:
        return None
    s = _strip_pump_suffix(addr.strip())
    if not is_probably_mint(s):
        # No spammeamos en WARNING aquí; dejamos WARNING al consumidor si quiere.
        return None
    if not is_valid_base58_32(s):
        # Mint con longitud razonable pero NO 32 bytes al decodificar (WrongSize típico).
        return None
    return s


def short_mint(s: str, left: int = 6, right: int = 4) -> str:
    """Forma corta amigable para logs: ABCDEF…WXYZ."""
    if not s:
        return s
    if len(s) <= left + right + 1:
        return s
    return f"{s[:left]}…{s[-right:]}"


__all__ = [
    "normalize_mint",
    "is_probably_mint",
    "is_valid_base58_32",
    "short_mint",
]
