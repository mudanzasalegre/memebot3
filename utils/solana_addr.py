# utils/solana_addr.py
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("solana_addr")

# Rango típico de longitud Base58 de mints SPL en Solana
_MIN_LEN = 30
_MAX_LEN = 50


def is_probably_mint(s: str | None) -> bool:
    """Heurística ligera: parece un mint SPL válido (no 0x, longitud 30–50)."""
    if not s:
        return False
    s = s.strip()
    if not s or s.startswith("0x"):
        return False
    return _MIN_LEN <= len(s) <= _MAX_LEN


def _strip_pump_suffix(addr: str) -> str:
    """Quita un sufijo literal 'pump' (muy común en feeds de Pump.fun)."""
    s = addr.strip()
    # Sólo si el sufijo es exactamente 'pump' (lowercase) y está al final.
    if s.endswith("pump"):
        cleaned = s[:-4]
        # Log de una sola línea para que quede constancia:
        try:
            log.debug("[addr] strip 'pump': %s → %s", s, cleaned)
        except Exception:
            pass
        return cleaned
    return s


def normalize_mint(addr: str | None) -> Optional[str]:
    """
    Devuelve un mint normalizado o None si no parece un mint SPL.
    - trim
    - quita sufijo 'pump' si está presente
    - valida heurísticamente longitud y no-0x
    """
    if not addr:
        return None
    s = _strip_pump_suffix(addr.strip())
    if not is_probably_mint(s):
        # No spammeamos en WARNING aquí; dejamos WARNING al consumidor si quiere.
        return None
    return s
