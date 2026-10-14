"""
Utilidades auxiliares desacopladas de negocio principal:

    from memebot2.utils import lista_pares, descubridor_pares
"""

from importlib import import_module
from types import ModuleType
from typing import Dict

_modules = ("lista_pares", "descubridor_pares", "logger")

globals_: Dict[str, ModuleType] = globals()


def __getattr__(name: str) -> ModuleType:
    if name not in _modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals_[name] = module
    return module


def __dir__() -> list[str]:
    return sorted(set(globals_) | set(_modules))

__all__ = list(_modules)
