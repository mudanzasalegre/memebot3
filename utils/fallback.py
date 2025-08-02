# memebot3/utils/fallback.py
"""
Utilidades genéricas de *fallback/merge* entre dos orígenes de datos.

`fill_missing_fields` se usa para completar claves que falten en el
diccionario primario con valores procedentes del diccionario secundario,
manteniendo la menor mutación posible y sin modificar los originales.

Ejemplo
-------
>>> primary   = {"liq_usd": None, "vol24h_usd": 5000}
>>> secondary = {"liq_usd": 32000, "mcap_usd": 120_000}
>>> out = fill_missing_fields(primary, secondary,
...                           fields=["liq_usd", "mcap_usd"])
>>> out
{'liq_usd': 32000, 'vol24h_usd': 5000, 'mcap_usd': 120000}

Se consideran “vacíos” los valores:
  • None
  • numpy.nan
  • 0 (opcional → ver parámetro `treat_zero_as_missing`)
"""
from __future__ import annotations

import copy
from typing import Dict, List, Any

import numpy as np

__all__ = ["fill_missing_fields"]


def _is_missing(val: Any, treat_zero_as_missing: bool = False) -> bool:
    """
    Devuelve True si `val` se considera hueco / ausente.
    • None
    • NaN (`float` o `numpy.nan`)
    • 0 (opcional, según flag)
    """
    if val is None:
        return True
    try:
        # numpy.isnan acepta floats y np.generic
        if isinstance(val, float) and np.isnan(val):
            return True
    except Exception:  # pragma: no cover
        pass
    if treat_zero_as_missing and val == 0:
        return True
    return False


def fill_missing_fields(
    primary: Dict[str, Any],
    secondary: Dict[str, Any],
    fields: List[str],
    *,
    treat_zero_as_missing: bool = False,
) -> Dict[str, Any]:
    """
    Rellena en `primary` los `fields` cuyo valor sea None/NaN (o 0 si
    `treat_zero_as_missing`) usando los correspondientes de `secondary`
    cuando existan.

    Devuelve **una copia** combinada: los diccionarios originales NO se
    modifican in-place.
    """
    merged = copy.deepcopy(primary)
    for f in fields:
        if _is_missing(merged.get(f), treat_zero_as_missing):
            sec_val = secondary.get(f)
            if not _is_missing(sec_val, treat_zero_as_missing):
                merged[f] = sec_val
    return merged
