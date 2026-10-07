"""
Entrada única para el sub-paquete *trader*:

    from trader import buyer, seller, gmgn

Paper imports do not initialize the live signer or require a wallet secret.
"""

from importlib import import_module
from types import ModuleType

_modules = ("gmgn", "sol_signer", "buyer", "seller", "papertrading")

def __getattr__(name: str) -> ModuleType:
    if name not in _modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module

__all__ = list(_modules)
