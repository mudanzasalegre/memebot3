"""Low-level scalar parsing: no feature-builder or trading imports."""
from __future__ import annotations

import numpy as np


def binary_value(value):
    """Return an exact binary observation or None, never Python truthiness."""
    if isinstance(value, (bool, np.bool_)):
        return int(value)
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"true", "yes", "y", "on", "t"}:
            return 1
        if value in {"false", "no", "n", "off", "f"}:
            return 0
    try:
        if value is None or isinstance(value, (dict, list, tuple, set, np.ndarray, complex, np.complexfloating)):
            return None
        number = float(value)
        return int(number) if np.isfinite(number) and number in (0., 1.) else None
    except (TypeError, ValueError, OverflowError):
        return None
