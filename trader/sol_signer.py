"""
Firma y envío de transacciones Solana con *solders*.

Mantiene tu implementación original, necesaria para main-net.  No requiere
modificación para el soporte de IA / DRY-RUN porque la lógica de “demo”
se controla antes de llegar aquí (gmgn.buy/sell no envía al firmante si
amount_sol ≤ 0).
"""
from __future__ import annotations

import base64
import base58
import json
import os
from typing import Final, Union

from solders.keypair import Keypair
from solders.pubkey import Pubkey as PublicKey
from solders.transaction import Transaction
from solana.rpc.api import Client

# ─────────────────────── variables entorno ────────────────────
RAW_SECRET: Final[str | None] = os.getenv("SOL_PRIVATE_KEY")
RPC_URL: Final[str] = os.getenv("SOL_RPC_URL", "https://api.mainnet-beta.solana.com")

if not RAW_SECRET:
    raise RuntimeError("Falta SOL_PRIVATE_KEY en .env")

# ───────────────────────── Helpers ─────────────────────────────
def _decode(raw: str) -> bytes:
    raw = raw.strip().split()[0]
    if raw.startswith("["):
        return bytes(json.loads(raw))
    try:
        return base58.b58decode(raw)
    except ValueError:
        return base64.b64decode(raw)


def _to_tx(obj: Union[str, bytes, Transaction]) -> Transaction:
    if isinstance(obj, Transaction):
        return obj
    if isinstance(obj, str):
        obj = base64.b64decode(obj)
    return Transaction.deserialize(obj)


# ───────────────────────── Keypair ─────────────────────────────
SECRET_BYTES: Final[bytes] = _decode(RAW_SECRET)

try:
    if len(SECRET_BYTES) == 64:
        KEYPAIR = Keypair.from_bytes(SECRET_BYTES)
    elif len(SECRET_BYTES) == 32:
        KEYPAIR = Keypair.from_seed(SECRET_BYTES)
    else:
        raise ValueError("Longitud de clave inválida")
except Exception as e:
    raise RuntimeError(f"No se pudo crear Keypair: {e}")

PUBLIC_KEY: Final[PublicKey] = KEYPAIR.pubkey()
client: Final[Client] = Client(RPC_URL)

# ───────────────────────── API Pública ─────────────────────────
def sign_and_send(tx: Union[str, bytes, Transaction]) -> str:
    """
    Firma y envía una transacción (base64 | bytes | Transaction).

    Devuelve la `signature` en base-58.
    """
    tx = _to_tx(tx)
    latest = client.get_latest_blockhash()["result"]["value"]["blockhash"]
    tx.recent_blockhash = latest
    tx.fee_payer = PUBLIC_KEY
    tx.sign([KEYPAIR])
    sig = client.send_raw_transaction(bytes(tx))
    return sig["result"]
