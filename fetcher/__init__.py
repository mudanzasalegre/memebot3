# fetcher/__init__.py
"""
Agrupa los wrappers de APIs externas; permite:

    from memebot3.fetcher import dexscreener, geckoterminal, pumpfun …

Además expone el alias de conveniencia:

    from fetcher import get_gt_data      # = geckoterminal.get_token_data
"""

from importlib import import_module
from types import ModuleType
from typing import Dict

# ───────────────────────── módulos públicos ─────────────────────────
_modules = (
    "dexscreener",
    "geckoterminal",     # ★ añadido
    "helius_cluster",
    "rugcheck",
    "pumpfun",
    "socials",
)

globals_: Dict[str, ModuleType] = globals()
for _m in _modules:
    globals_[_m] = import_module(f"{__name__}.{_m}")

# Alias explícito para facilitar el import en test y servicios
from .geckoterminal import get_token_data as get_gt_data  # type: ignore

__all__ = list(_modules) + ["get_gt_data"]
