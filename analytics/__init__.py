"""
Paquete de señales y scoring.

    from memebot2.analytics import filters, trend, insider
"""

from importlib import import_module
from types import ModuleType
from typing import Dict

_modules = ("filters", "trend", "insider")

globals_: Dict[str, ModuleType] = globals()
for _m in _modules:
    globals_[_m] = import_module(f"{__name__}.{_m}")

__all__ = list(_modules)
