"""Bounded read-only original-transaction evidence; never broadcasts or signs."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from urllib.parse import urlsplit

import aiohttp

from execution import chain_reconciliation as chain
from utils.solana_rpc import _rpc_urls


def checked_endpoint(url):
    p = urlsplit(url)
    if (not p.hostname or p.username or p.password or p.fragment or p.hostname in {"api.jup.ag", "gmgn.ai"}
            or (p.scheme != "https" and not (p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError("Invalid read-only Solana RPC endpoint")
    if p.port is not None and not 1 <= p.port <= 65535:
        raise ValueError("Invalid read-only Solana RPC port")
    return url


def configured_endpoint():
    return checked_endpoint(_rpc_urls()[0])


def endpoint_fingerprint(url):
    return hashlib.sha256(checked_endpoint(url).encode()).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous duplicate RPC JSON field")
        result[key] = value
    return result


async def fetch_transaction_evidence(signature, *, endpoint=None):
    """Two uncached queries to the same configured node, no trade retry.

    Each request has an 8-second total deadline. No provider API key/header is
    forwarded and no response/body/endpoint is included in exceptions or logs.
    A missing transaction or failed observation is unknown, never no-fill.
    """
    url = configured_endpoint() if endpoint is None else checked_endpoint(endpoint)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        async def read(method, params):
            identity = uuid.uuid4().hex
            payload = {"jsonrpc": "2.0", "id": identity, "method": method, "params": params}
            async with session.post(url, json=payload, allow_redirects=False) as response:
                if response.status != 200:
                    raise RuntimeError("Original transaction RPC observation unavailable")
                chunks, size = [], 0
                while True:
                    chunk = await response.content.read(min(65536, 2 * 1024 * 1024 + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 2 * 1024 * 1024:
                        raise ValueError("Original transaction RPC response exceeds bound")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                result = json.loads(raw, object_pairs_hook=_unique_object)
                if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0"
                        or result.get("id") != identity or ("error" in result and result["error"] is not None) or "result" not in result):
                    raise ValueError("Original transaction RPC envelope is invalid")
                return copy.deepcopy(result["result"])
        tx = await read("getTransaction", [signature, {"encoding": "base64",
            "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
        status = await read("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
    return tx, status


async def reconcile_original(capsule, execution, *, endpoint):
    if endpoint_fingerprint(endpoint) != capsule["rpc_source_sha256"]:
        raise ValueError("Observation route changed after original preparation")
    signature = execution["signature"]
    tx, statuses = await fetch_transaction_evidence(signature, endpoint=endpoint)
    return chain.reconcile(capsule, execution, tx, statuses)
