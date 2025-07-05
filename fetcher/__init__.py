"""
Agrupa los wrappers de APIs externas para poder hacer:

    from memebot2.fetcher import dexscreener, pumpfun, rugcheck …

`__all__` expone sólo los módulos públicos.
"""

from importlib import import_module
from types import ModuleType
from typing import Dict

_modules = (
    "dexscreener",
    "helius_cluster",
    "rugcheck",
    "pumpfun",
    "socials",
)

globals_: Dict[str, ModuleType] = globals()
for _m in _modules:
    globals_[_m] = import_module(f"{__name__}.{_m}")

__all__ = list(_modules)
