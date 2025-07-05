"""
Puente ligero a la Cooperation-API “GMGN” (+ firma local).

Flujo BUY / SELL:
1. Pide la ruta + raw_tx (unsigned) a
   https://gmgn.ai/defi/router/v1/sol/tx/get_swap_route
2. Firma localmente con *solders* (`sol_signer.sign_b64`)
3. Envía la transacción firmada a la red vía RPC
   (lo hace `sol_signer.sign_and_send()`).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

import aiohttp
import tenacity

from config import exits              # ← sin cambios: módulos de riesgo
from . import sol_signer

log = logging.getLogger("gmgn")

# ───────────── Constantes & helpers ────────────────────────────
GMGN_HOST = "https://gmgn.ai"
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000  # 1 SOL = 1e9 lamports


def _pretty(d: Dict[str, Any]) -> str:
    return json.dumps(d, separators=(",", ":"))


async def _route(
    token_in: str,
    token_out: str,
    lamports_in: int,
    from_addr: str,
    slippage: float = 2.0,
) -> dict:
    """
    Llama al endpoint de ruta y devuelve el JSON completo.
    """
    url = (
        f"{GMGN_HOST}/defi/router/v1/sol/tx/get_swap_route?"
        f"token_in_address={token_in}"
        f"&token_out_address={token_out}"
        f"&in_amount={lamports_in}"
        f"&from_address={from_addr}"
        f"&slippage={slippage}"
    )
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=20) as r:
            r.raise_for_status()
            return await r.json()


# ───────────── Operaciones públicas ────────────────────────────
@tenacity.retry(wait=tenacity.wait_fixed(2), stop=tenacity.stop_after_attempt(3))
async def buy(token_addr: str, amount_sol: float) -> dict:
    """
    Compra *amount_sol* del token *token_addr*.

    Devuelve {"route":<json>, "signature":<sig_b58>}
    """
    if amount_sol <= 0:
        log.info("[GMGN] Simulación BUY – amount=0")
        return {"route": {}, "signature": "SIMULATION"}

    owner = sol_signer.PUBLIC_KEY.to_string()
    lamports_in = int(amount_sol * LAMPORTS)

    route = await _route(SOL_MINT, token_addr, lamports_in, owner)
    unsigned_b64 = route["data"]["raw_tx"]["swapTransaction"]

    sig = sol_signer.sign_and_send(unsigned_b64)
    log.info(
        "[GMGN] BUY %.3f SOL → %s  sig=%s",
        amount_sol,
        token_addr,
        sig[:6],
    )
    return {"route": route, "signature": sig}


@tenacity.retry(wait=tenacity.wait_fixed(2), stop=tenacity.stop_after_attempt(3))
async def sell(token_addr: str, qty_lamports: int) -> dict:
    """
    Vende *qty_lamports* unidades (lamports del SPL) del token *token_addr*.

    Devuelve {"route":<json>, "signature":<sig_b58>}
    """
    if qty_lamports <= 0:
        log.info("[GMGN] Simulación SELL – qty=0")
        return {"route": {}, "signature": "SIMULATION"}

    owner = sol_signer.PUBLIC_KEY.to_string()

    route = await _route(token_addr, SOL_MINT, qty_lamports, owner)
    unsigned_b64 = route["data"]["raw_tx"]["swapTransaction"]

    sig = sol_signer.sign_and_send(unsigned_b64)
    log.info(
        "[GMGN] SELL %.0f lamports %s  sig=%s",
        qty_lamports,
        token_addr,
        sig[:6],
    )
    return {"route": route, "signature": sig}
