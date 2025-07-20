# memebot3/utils/time.py
"""
Pequeño utilitario de tiempo.

`utc_now()`  →  datetime timezone-aware en UTC
"""
from datetime import datetime, timezone


def utc_now():
    """Shorthand para `datetime.now(timezone.utc)` (aware)."""
    return datetime.now(timezone.utc)
