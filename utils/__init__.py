"""
Utilidades auxiliares desacopladas de negocio principal:

    from memebot2.utils import lista_pares, descubridor_pares
"""

from importlib import import_module
from types import ModuleType
from typing import Dict

_modules = ("lista_pares", "descubridor_pares", "logger")

globals_: Dict[str, ModuleType] = globals()
for _m in _modules:
    globals_[_m] = import_module(f"{__name__}.{_m}")

__all__ = list(_modules)