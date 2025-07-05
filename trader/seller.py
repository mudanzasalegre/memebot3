"""
Wrapper de gmgn.sell con parsing homogéneo:

Devuelve:
    {
      signature : str
      route     : dict   # raw JSON de GMGN
    }
"""
from __future__ import annotations

import logging
from typing import Dict

from . import gmgn

log = logging.getLogger("seller")


async def sell(token_addr: str, qty_lamports: int) -> Dict[str, object]:
    if qty_lamports <= 0:
        log.warning("[seller] Qty=0 — orden ignorada")
        return {"signature": "NO_QTY", "route": {}}

    resp = await gmgn.sell(token_addr, qty_lamports)
    return {
        "signature": resp.get("signature"),
        "route": resp.get("route", {}),
    }
