"""
Entrada única para el sub-paquete *trader*:

    from memebot2.trader import buyer, seller, gmgn
"""

from importlib import import_module
from types import ModuleType
from typing import Dict

_modules = ("gmgn", "sol_signer", "buyer", "seller")

globals_: Dict[str, ModuleType] = globals()
for _m in _modules:
    globals_[_m] = import_module(f"{__name__}.{_m}")

__all__ = list(_modules)
