"""
Signing and broadcast helpers for Solana transactions.

This module keeps backward compatibility with the previous public contract:
  - KEYPAIR
  - PUBLIC_KEY
  - client
  - sign_and_send(...)

It now also supports:
  - private RPC preference with fallbacks
  - base64/raw signing helpers for managed Jupiter order/execute flows
  - optional Jito block-engine broadcast for pre-signed transactions
"""
from __future__ import annotations

import base64
import base58
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Final, Iterable, Union

from solders.keypair import Keypair
from solders.pubkey import Pubkey as PublicKey
from solders.transaction import Transaction
from solders.hash import Hash
from solders.message import Message, to_bytes_versioned
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.types import TxOpts


RAW_SECRET: Final[str | None] = os.getenv("SOL_PRIVATE_KEY")
if not RAW_SECRET:
    raise RuntimeError("Falta SOL_PRIVATE_KEY en .env")


def _decode_secret(raw: str) -> bytes:
    text = raw.strip()
    if text.startswith("["):
        values = json.loads(text)
        if not isinstance(values, list) or any(type(value) is not int or not 0 <= value <= 255 for value in values):
            raise ValueError("Invalid key byte array")
        return bytes(values)
    try:
        decoded = base58.b58decode(text)
        if len(decoded) in (32, 64):
            return decoded
    except ValueError:
        pass
    return base64.b64decode(text, validate=True)


def _csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _rpc_urls() -> list[str]:
    default_rpc = "https://api.mainnet-beta.solana.com"
    primary = (os.getenv("SOL_RPC_URL", "") or "").strip()
    helius = (os.getenv("HELIUS_RPC_URL", "") or "").strip()
    generic = (os.getenv("RPC_URL", "") or "").strip()
    extra = _csv(os.getenv("SOL_RPC_FALLBACKS", ""))
    use_private_first = (os.getenv("USE_PRIVATE_RPC_FIRST", "true") or "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    ordered: list[str] = []
    if use_private_first and helius:
        ordered.append(helius)
    if primary:
        ordered.append(primary)
    if helius and helius not in ordered:
        ordered.append(helius)
    if generic and generic not in ordered:
        ordered.append(generic)
    ordered.extend(extra)
    ordered.append(default_rpc)
    return _dedupe(ordered)


SECRET_BYTES: Final[bytes] = _decode_secret(RAW_SECRET)
if len(SECRET_BYTES) == 64:
    KEYPAIR = Keypair.from_bytes(SECRET_BYTES)
elif len(SECRET_BYTES) == 32:
    KEYPAIR = Keypair.from_seed(SECRET_BYTES)
else:
    raise RuntimeError("No se pudo crear Keypair: longitud de clave invalida")

PUBLIC_KEY: Final[PublicKey] = KEYPAIR.pubkey()
RPC_URLS: Final[list[str]] = _rpc_urls()
RPC_URL: Final[str] = RPC_URLS[0]
client: Final[Client] = Client(RPC_URL)

JITO_BLOCK_ENGINE_URL: Final[str] = (
    os.getenv("JITO_BLOCK_ENGINE_URL", "https://ny.mainnet.block-engine.jito.wtf").rstrip("/")
)
JITO_UUID: Final[str] = (os.getenv("JITO_UUID", "") or "").strip()
JITO_BROADCAST_ENABLED: Final[bool] = (
    (os.getenv("JITO_BROADCAST_ENABLED", "false") or "false").strip().lower()
    in {"1", "true", "yes", "on"}
)
JITO_BUNDLE_ONLY: Final[bool] = (
    (os.getenv("JITO_BUNDLE_ONLY", "true") or "true").strip().lower()
    in {"1", "true", "yes", "on"}
)


def _to_raw_bytes(obj: Union[str, bytes, Transaction]) -> bytes:
    if isinstance(obj, Transaction):
        return bytes(obj)
    if isinstance(obj, bytes):
        return obj
    text = obj.strip()
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"Transaccion base64 invalida: {exc}") from exc


def _client_for_url(url: str) -> Client:
    if url == RPC_URL:
        return client
    return Client(url)


def _send_via_rpc(signed_bytes: bytes, *, skip_preflight: bool = False) -> str:
    expected_signature = _transaction_signature(signed_bytes)
    last_error: Exception | None = None
    for rpc_url in RPC_URLS:
        try:
            rpc_client = _client_for_url(rpc_url)
            resp = rpc_client.send_raw_transaction(
                signed_bytes,
                opts=TxOpts(skip_preflight=bool(skip_preflight)),
            )
            result = getattr(resp, "value", None)
            if result is None and isinstance(resp, dict):
                if resp.get("error"):
                    raise RuntimeError("RPC returned an error without original transaction acknowledgement")
                result = resp.get("result")
            if result is not None and str(result) == expected_signature:
                return expected_signature
            raise RuntimeError("RPC did not acknowledge the original transaction signature")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    raise RuntimeError("RPC broadcast unconfirmed; retain the original transaction") from last_error


def _send_via_jito(signed_bytes: bytes, *, bundle_only: bool | None = None) -> str:
    expected_signature = _transaction_signature(signed_bytes)
    bundle_flag = JITO_BUNDLE_ONLY if bundle_only is None else bool(bundle_only)
    path = "/api/v1/transactions"
    query = "?bundleOnly=true" if bundle_flag else ""
    url = f"{JITO_BLOCK_ENGINE_URL}{path}{query}"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            base64.b64encode(signed_bytes).decode("ascii"),
            {"encoding": "base64"},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if JITO_UUID:
        headers["x-jito-auth"] = JITO_UUID

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=8) as response:
        body = response.read().decode("utf-8")
        try:
            data = json.loads(body or "{}")
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Jito returned malformed acknowledgement JSON") from exc
        if not isinstance(data, dict) or data.get("error"):
            raise RuntimeError("Jito returned no original transaction acknowledgement")
        result = data.get("result")
        if not result or str(result) != expected_signature:
            raise RuntimeError("Jito did not acknowledge the original transaction signature")
        return expected_signature


def sign_raw_transaction(raw_tx: bytes) -> bytes:
    vtx = VersionedTransaction.from_bytes(raw_tx)  # Supports legacy and v0.
    required = vtx.message.header.num_required_signatures
    signers = list(vtx.message.account_keys[:required])
    if KEYPAIR.pubkey() not in signers or len(vtx.signatures) != required:
        raise ValueError("Configured wallet is not an original required signer")
    signatures = list(vtx.signatures)
    # Versioned signing includes the version prefix. Preserve fee-payer and
    # other original signatures; managed providers may supply a co-signature.
    signatures[signers.index(KEYPAIR.pubkey())] = KEYPAIR.sign_message(to_bytes_versioned(vtx.message))
    return bytes(VersionedTransaction.populate(vtx.message, signatures))


def _transaction_signature(signed_bytes: bytes) -> str:
    tx = VersionedTransaction.from_bytes(signed_bytes)
    if not tx.signatures or tx.signatures[0] == Signature.default():
        raise ValueError("Transaction has no original fee-payer signature")
    return str(tx.signatures[0])


def sign_base64_transaction(tx_b64: str) -> str:
    signed = sign_raw_transaction(base64.b64decode(tx_b64, validate=True))
    return base64.b64encode(signed).decode("ascii")


def send_raw_transaction(
    signed: Union[str, bytes],
    *,
    skip_preflight: bool = False,
    prefer_jito: bool | None = None,
    bundle_only: bool | None = None,
) -> str:
    signed_bytes = signed if isinstance(signed, bytes) else base64.b64decode(signed, validate=True)
    tx = VersionedTransaction.from_bytes(signed_bytes)
    if not tx.signatures or not all(tx.verify_with_results()):
        raise ValueError("RPC broadcast requires all original transaction signatures")
    try_jito = JITO_BROADCAST_ENABLED if prefer_jito is None else bool(prefer_jito)
    if try_jito:
        try:
            return _send_via_jito(signed_bytes, bundle_only=bundle_only)
        except (OSError, urllib.error.URLError, RuntimeError):
            pass
    return _send_via_rpc(signed_bytes, skip_preflight=skip_preflight)


def sign_and_send(
    tx: Union[str, bytes, Transaction],
    *,
    skip_preflight: bool = False,
    prefer_jito: bool | None = None,
    bundle_only: bool | None = None,
) -> str:
    """
    Sign and broadcast a transaction.

    - Single-signer `Transaction` objects refresh their blockhash once before signing.
      Preserve the original account/instruction layout; never change fee payer.
    - `bytes` / base64 strings are treated as prebuilt Solana transactions and are signed as-is.
    """
    if isinstance(tx, Transaction):
        message = tx.message
        header = message.header
        if header.num_required_signatures != 1 or message.account_keys[0] != KEYPAIR.pubkey():
            raise ValueError("Blockhash refresh requires one original configured signer")
        last_error: Exception | None = None
        blockhash = None
        for rpc_url in RPC_URLS:
            try:
                rpc_client = _client_for_url(rpc_url)
                latest = rpc_client.get_latest_blockhash()
                value = getattr(latest, "value", None)
                candidate = getattr(value, "blockhash", None)
                if candidate is None and isinstance(latest, dict):
                    candidate = latest["result"]["value"]["blockhash"]
                blockhash = candidate if isinstance(candidate, Hash) else Hash.from_string(str(candidate))
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        if blockhash is None:
            raise RuntimeError("No valid blockhash available before submission") from last_error
        refreshed = Message.new_with_compiled_instructions(header.num_required_signatures,
            header.num_readonly_signed_accounts, header.num_readonly_unsigned_accounts,
            message.account_keys, blockhash, message.instructions)
        signed = sign_raw_transaction(bytes(Transaction.new_unsigned(refreshed)))
        # All endpoint retries after this boundary relay exactly these bytes.
        return send_raw_transaction(signed, skip_preflight=skip_preflight,
                                    prefer_jito=prefer_jito, bundle_only=bundle_only)

    raw_tx = _to_raw_bytes(tx)
    signed = sign_raw_transaction(raw_tx)
    return send_raw_transaction(
        signed,
        skip_preflight=skip_preflight,
        prefer_jito=prefer_jito,
        bundle_only=bundle_only,
    )


__all__ = [
    "KEYPAIR",
    "PUBLIC_KEY",
    "RPC_URL",
    "RPC_URLS",
    "client",
    "send_raw_transaction",
    "sign_and_send",
    "sign_base64_transaction",
    "sign_raw_transaction",
]
