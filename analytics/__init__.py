"""
Paquete de señales y scoring.

    from memebot2.analytics import filters, trend, insider
"""

from importlib import import_module
from types import ModuleType
from typing import Dict

_modules = ("filters", "trend", "insider", "requeue_policy", "sizing", "exit_policy")


def __getattr__(name: str) -> ModuleType:
    if name not in _modules:
        raise AttributeError(name)
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module

__all__ = list(_modules)
