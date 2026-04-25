from __future__ import annotations

import logging
import re
from typing import Dict, Optional

log = logging.getLogger("solana_addr")

# Typical Base58 length range for SPL token mints on Solana.
_MIN_LEN = 30
_MAX_LEN = 50
_CACHE_MAX = 8192

# Cache full normalization results to avoid repeated Base58 decoding and log spam.
_NORMALIZE_CACHE: Dict[str, Optional[str]] = {}
# Cache sanitation logs so the same raw input is only reported once.
_SANITIZE_LOG_CACHE: Dict[str, str] = {}

_DELIMITED_PUMP_SUFFIX_RE = re.compile(
    r"^(?P<mint>[1-9A-HJ-NP-Za-km-z]+?)(?:[._\-\s:/]+pump)$",
    re.IGNORECASE,
)

# Base58 is optional, but preferred for strict 32-byte validation.
try:
    from base58 import b58decode as _b58decode
except Exception:  # pragma: no cover - environment without dependency
    _b58decode = None  # type: ignore[attr-defined]
    _BASE58_IMPORT_OK = False
else:  # pragma: no cover
    _BASE58_IMPORT_OK = True


def _cache_set(cache: Dict[str, Optional[str] | str], key: str, value: Optional[str] | str) -> None:
    if key not in cache and len(cache) >= _CACHE_MAX:
        cache.clear()
    cache[key] = value


def _log_sanitize_once(raw: str, cleaned: str, reason: str) -> None:
    cached = _SANITIZE_LOG_CACHE.get(raw)
    if cached == cleaned:
        return
    try:
        log.debug("[addr] normalize %s: %s -> %s", reason, raw, cleaned)
    except Exception:
        pass
    _cache_set(_SANITIZE_LOG_CACHE, raw, cleaned)


def is_probably_mint(s: str | None) -> bool:
    """
    Cheap heuristic: looks like an SPL mint (not 0x, length 30-50).

    This does not guarantee a valid 32-byte public key. Use
    `is_valid_base58_32()` for strict validation.
    """
    if not s:
        return False
    s = s.strip()
    if not s or s.startswith("0x"):
        return False
    return _MIN_LEN <= len(s) <= _MAX_LEN


def is_valid_base58_32(s: str) -> bool:
    """
    Validate that `s`:
      1) is valid Base58
      2) decodes to exactly 32 bytes
    """
    if not _BASE58_IMPORT_OK:
        return is_probably_mint(s)

    try:
        decoded = _b58decode(s)  # type: ignore[misc]
    except Exception:
        return False
    return len(decoded) == 32


def _is_valid_mint(s: str) -> bool:
    return is_probably_mint(s) and is_valid_base58_32(s)


def _fallback_cleanup_invalid_mint(raw: str) -> tuple[Optional[str], Optional[str]]:
    """
    Try controlled cleanup variants, but only for raw inputs that already failed
    strict validation.

    Important: a bare trailing "pump" is Base58-compatible, so a valid mint can
    legitimately end with it. We only try to strip it if the original raw input
    is invalid and the cleaned version becomes a valid mint.
    """
    match = _DELIMITED_PUMP_SUFFIX_RE.fullmatch(raw)
    if match:
        return match.group("mint"), "delimited_pump_suffix"

    if raw.lower().endswith("pump") and len(raw) > 4:
        return raw[:-4], "pump_suffix_fallback"

    return None, None


def normalize_mint(addr: str | None) -> Optional[str]:
    """
    Return a normalized mint or None if the input is not a valid SPL mint.

    Behavior:
      - trim the raw input
      - accept a valid raw mint unchanged
      - only if raw is invalid, try controlled cleanup variants such as:
        * delimiter suffixes like "_pump", ".pump", "-pump"
        * bare "pump" suffix fallback
      - accept the cleaned candidate only if it becomes a valid 32-byte Base58 key
    """
    if not addr:
        return None

    raw = addr.strip()
    if not raw:
        return None

    if raw in _NORMALIZE_CACHE:
        return _NORMALIZE_CACHE[raw]

    if _is_valid_mint(raw):
        _cache_set(_NORMALIZE_CACHE, raw, raw)
        return raw

    cleaned, reason = _fallback_cleanup_invalid_mint(raw)
    if cleaned and _is_valid_mint(cleaned):
        _log_sanitize_once(raw, cleaned, reason or "fallback")
        _cache_set(_NORMALIZE_CACHE, raw, cleaned)
        return cleaned

    _cache_set(_NORMALIZE_CACHE, raw, None)
    return None


def short_mint(s: str, left: int = 6, right: int = 4) -> str:
    """Human-friendly short form for logs: ABCDEF...WXYZ."""
    if not s:
        return s
    if len(s) <= left + right + 1:
        return s
    return f"{s[:left]}...{s[-right:]}"


__all__ = [
    "normalize_mint",
    "is_probably_mint",
    "is_valid_base58_32",
    "short_mint",
]
