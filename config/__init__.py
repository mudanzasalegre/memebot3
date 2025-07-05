"""
Acceso rápido a la configuración:

    from config import CFG, exits, MAX_AGE_DAYS

Re-exporta todo lo definido en config.config y el sub-módulo exits.
"""
from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Dict

_cfg_mod: ModuleType = import_module("config.config")
globals().update(_cfg_mod.__dict__)          # expone CFG y las constantes
exits: ModuleType = import_module("config.exits")  # noqa: F401

# ── construye __all__ con los símbolos públicos de config.config + exits
__all__: list[str] = [
    name for name in _cfg_mod.__dict__ if not name.startswith("_")
] + ["exits"]
