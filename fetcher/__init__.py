# memebot3/fetcher/__init__.py
"""
Agrupa los wrappers de APIs externas y facilita su importación:

    from memebot3.fetcher import dexscreener, geckoterminal, pumpfun …

Además expone alias de conveniencia:

    from fetcher import get_gt_data      # ≡ geckoterminal.get_token_data
"""

from importlib import import_module
from types import ModuleType
from typing import Dict

# ───────────────────────── módulos públicos ─────────────────────────
_modules = (
    "dexscreener",
    "geckoterminal",     # ★ wrapper GeckoTerminal
    "helius_cluster",
    "rugcheck",
    "pumpfun",
    "socials",
    "jupiter_price",     # ★ NUEVO: Price API v3 (Lite) de Jupiter
)

globals_: Dict[str, ModuleType] = globals()
for _m in _modules:
    globals_[_m] = import_module(f"{__name__}.{_m}")

# Alias explícito para facilitar el import en tests y servicios externos
from .geckoterminal import get_token_data as get_gt_data  # type: ignore

__all__ = list(_modules) + ["get_gt_data"]
