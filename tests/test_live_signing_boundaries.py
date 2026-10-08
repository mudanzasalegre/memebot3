"""Actual SDK signatures using synthetic keys and entirely fake HTTP/RPC."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
import tenacity
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message, MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction, VersionedTransaction
from solders.rpc.responses import GetLatestBlockhashResp, SendTransactionResp
from solders.system_program import transfer, TransferParams

from trader import gmgn


@pytest.fixture
def signer(monkeypatch):
    import solana.rpc.api
    # Never import the operator's signing module/key. Only this detached module
    # receives a public synthetic fixture seed. RPC construction is inert.
    monkeypatch.setenv("SOL_PRIVATE_KEY", json.dumps(list(range(32))))
    monkeypatch.setenv("SOL_RPC_URL", "https://synthetic.invalid")
    for name in ("HELIUS_RPC_URL", "RPC_URL", "SOL_RPC_FALLBACKS", "JITO_UUID"):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(solana.rpc.api, "Client", lambda *args, **kwargs: SimpleNamespace())
    path = Path(__file__).resolve().parents[1] / "trader" / "sol_signer.py"
    spec = importlib.util.spec_from_file_location("isolated_synthetic_signer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "RPC_URLS", ["rpc1", "rpc2"])
    monkeypatch.setattr(module, "JITO_BROADCAST_ENABLED", False)
    return module


def unsigned(owner, version):
    blockhash = Hash.new_unique()
    if version == "legacy":
        return bytes(Transaction.new_unsigned(Message.new_with_blockhash([], owner, blockhash)))
    msg = MessageV0.try_compile(owner, [], [], blockhash)
    return bytes(VersionedTransaction.populate(msg, [Signature.default()]))


def modern_blockhash(value):
    return GetLatestBlockhashResp.from_json(json.dumps({"jsonrpc": "2.0", "id": 1,
        "result": {"context": {"slot": 123}, "value": {"blockhash": str(value), "lastValidBlockHeight": 100}}}))


@pytest.mark.parametrize("version", ["legacy", "v0"])
def test_actual_signatures_verify_and_original_message_is_unchanged(signer, version):
    raw = unsigned(signer.PUBLIC_KEY, version)
    original = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction.from_bytes(signer.sign_raw_transaction(raw))
    assert signed.message == original.message
    assert signed.verify_with_results() == [True]
    assert signed.signatures[0].verify(signer.PUBLIC_KEY, to_bytes_versioned(signed.message))
    encoded = signer.sign_base64_transaction(base64.b64encode(raw).decode())
    assert base64.b64decode(encoded) == bytes(signed)


def test_sponsored_v0_signs_actual_wallet_index_and_preserves_sponsor(signer):
    sponsor = Keypair.from_seed(bytes([7]) * 32)
    ix = Instruction(Pubkey.new_unique(), b"synthetic", [AccountMeta(signer.PUBLIC_KEY, True, False)])
    msg = MessageV0.try_compile(sponsor.pubkey(), [ix], [], Hash.new_unique())
    signatures = [sponsor.sign_message(to_bytes_versioned(msg)), Signature.default()]
    raw = bytes(VersionedTransaction.populate(msg, signatures))
    signed = VersionedTransaction.from_bytes(signer.sign_raw_transaction(raw))
    assert signed.signatures[0] == signatures[0] and signed.verify_with_results() == [True, True]


def test_managed_partial_signature_is_preserved_but_not_locally_broadcast(signer, monkeypatch):
    sponsor = Keypair.from_seed(bytes([7]) * 32)
    ix = Instruction(Pubkey.new_unique(), b"synthetic", [AccountMeta(signer.PUBLIC_KEY, True, False)])
    msg = MessageV0.try_compile(sponsor.pubkey(), [ix], [], Hash.new_unique())
    raw = bytes(VersionedTransaction.populate(msg, [Signature.default(), Signature.default()]))
    signed = signer.sign_raw_transaction(raw)
    assert VersionedTransaction.from_bytes(signed).verify_with_results() == [False, True]
    rpc = Mock()
    monkeypatch.setattr(signer, "_send_via_rpc", rpc)
    with pytest.raises(ValueError, match="all original"):
        signer.send_raw_transaction(signed)
    rpc.assert_not_called()


@pytest.mark.parametrize("version", ["legacy", "v0"])
def test_foreign_signer_rejected_before_rpc(signer, version):
    with pytest.raises(ValueError, match="required signer"):
        signer.sign_raw_transaction(unsigned(Pubkey.new_unique(), version))


@pytest.mark.parametrize("shape", ["modern", "dictionary"])
def test_blockhash_fallback_occurs_only_before_one_signature_and_identical_broadcast(signer, monkeypatch, shape):
    ix = transfer(TransferParams(from_pubkey=signer.PUBLIC_KEY, to_pubkey=Pubkey.new_unique(), lamports=1))
    old = Message.new_with_blockhash([ix], signer.PUBLIC_KEY, Hash.new_unique())
    tx = Transaction.new_unsigned(old)
    fresh = Hash.new_unique()
    first, second = Mock(), Mock()
    first.get_latest_blockhash.side_effect = TimeoutError("synthetic pre-send failure")
    second.get_latest_blockhash.return_value = (modern_blockhash(fresh)
        if shape == "modern" else {"result": {"value": {"blockhash": str(fresh)}}})
    sent = []

    def send1(raw, **kwargs):
        sent.append(raw)
        raise TimeoutError("synthetic acknowledgement lost after send")

    def send2(raw, **kwargs):
        sent.append(raw)
        return SendTransactionResp.from_json(json.dumps({"jsonrpc": "2.0", "id": 1,
            "result": str(VersionedTransaction.from_bytes(raw).signatures[0])}))

    first.send_raw_transaction.side_effect = send1
    second.send_raw_transaction.side_effect = send2
    monkeypatch.setattr(signer, "_client_for_url", lambda url: first if url == "rpc1" else second)
    sign = Mock(wraps=signer.sign_raw_transaction)
    monkeypatch.setattr(signer, "sign_raw_transaction", sign)
    result = signer.sign_and_send(tx)
    assert len(sent) == 2 and sent[0] == sent[1] and sign.call_count == 1
    actual = VersionedTransaction.from_bytes(sent[0])
    assert actual.message.recent_blockhash == fresh and actual.verify_with_results() == [True]
    assert actual.message.header == old.header and actual.message.account_keys == old.account_keys
    assert actual.message.instructions == old.instructions
    assert tx.message == old  # no caller-object mutation or new intent
    assert result == str(actual.signatures[0])
    first.get_latest_blockhash.assert_called_once()
    second.get_latest_blockhash.assert_called_once()


def test_all_ambiguous_broadcasts_do_not_refresh_or_resign(signer, monkeypatch):
    client = Mock()
    client.get_latest_blockhash.return_value = modern_blockhash(Hash.new_unique())
    client.send_raw_transaction.side_effect = TimeoutError("synthetic")
    monkeypatch.setattr(signer, "_client_for_url", lambda url: client)
    tx = Transaction.new_unsigned(Message.new_with_blockhash([], signer.PUBLIC_KEY, Hash.new_unique()))
    with pytest.raises(RuntimeError, match="unconfirmed"):
        signer.sign_and_send(tx)
    client.get_latest_blockhash.assert_called_once()
    assert client.send_raw_transaction.call_count == 2
    assert client.send_raw_transaction.call_args_list[0].args == client.send_raw_transaction.call_args_list[1].args


@pytest.mark.parametrize("value", [None, "invalid-hash", {"invalid": True}])
def test_invalid_blockhash_never_reaches_signing_or_broadcast(signer, monkeypatch, value):
    client = Mock()
    client.get_latest_blockhash.return_value = SimpleNamespace(value=SimpleNamespace(blockhash=value))
    monkeypatch.setattr(signer, "_client_for_url", lambda url: client)
    tx = Transaction.new_unsigned(Message.new_with_blockhash([], signer.PUBLIC_KEY, Hash.new_unique()))
    with pytest.raises(RuntimeError, match="No valid blockhash"):
        signer.sign_and_send(tx)
    client.send_raw_transaction.assert_not_called()


@pytest.mark.parametrize("value", [Signature.default(), None, True, "", "unrelated"])
def test_wrong_rpc_acknowledgement_keeps_original_identity(signer, monkeypatch, value):
    raw = signer.sign_raw_transaction(unsigned(signer.PUBLIC_KEY, "v0"))
    client = Mock()
    client.send_raw_transaction.return_value = SimpleNamespace(value=value)
    monkeypatch.setattr(signer, "_client_for_url", lambda url: client)
    with pytest.raises(RuntimeError, match="unconfirmed"):
        signer.send_raw_transaction(raw)
    assert client.send_raw_transaction.call_count == 2


def test_jito_ambiguous_failure_relays_same_signed_packet_to_rpc(signer, monkeypatch):
    raw = signer.sign_raw_transaction(unsigned(signer.PUBLIC_KEY, "v0"))
    jito = Mock(side_effect=TimeoutError("synthetic acknowledgement lost"))
    rpc = Mock(return_value=str(VersionedTransaction.from_bytes(raw).signatures[0]))
    monkeypatch.setattr(signer, "_send_via_jito", jito)
    monkeypatch.setattr(signer, "_send_via_rpc", rpc)
    assert signer.send_raw_transaction(raw, prefer_jito=True) == rpc.return_value
    assert jito.call_args.args[0] == rpc.call_args.args[0] == raw


@pytest.mark.parametrize("body", ["not JSON", "[]", '{"error":"synthetic"}', '{"result":"wrong-signature"}'])
def test_actual_jito_bad_ack_falls_back_with_unchanged_packet(signer, monkeypatch, body):
    raw = signer.sign_raw_transaction(unsigned(signer.PUBLIC_KEY, "v0"))

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return body.encode()

    sent = []
    def urlopen(request, **kwargs):
        sent.append(json.loads(request.data)["params"][0])
        return Response()
    monkeypatch.setattr(signer.urllib.request, "urlopen", urlopen)
    rpc = Mock(return_value=str(VersionedTransaction.from_bytes(raw).signatures[0]))
    monkeypatch.setattr(signer, "_send_via_rpc", rpc)
    assert signer.send_raw_transaction(raw, prefer_jito=True) == rpc.return_value
    assert base64.b64decode(sent[0]) == rpc.call_args.args[0] == raw


@pytest.mark.parametrize("error", [False, True])
def test_legacy_dictionary_rpc_ack_requires_matching_signature_and_no_error(signer, monkeypatch, error):
    raw = signer.sign_raw_transaction(unsigned(signer.PUBLIC_KEY, "v0"))
    signature = str(VersionedTransaction.from_bytes(raw).signatures[0])
    client = Mock()
    client.send_raw_transaction.return_value = {"result": signature, **({"error": "synthetic"} if error else {})}
    monkeypatch.setattr(signer, "_client_for_url", lambda url: client)
    if error:
        with pytest.raises(RuntimeError, match="unconfirmed"):
            signer.send_raw_transaction(raw)
    else:
        assert signer.send_raw_transaction(raw) == signature


def test_refreshed_legacy_multisigner_rejected_before_rpc(signer, monkeypatch):
    other = Pubkey.new_unique()
    ix = Instruction(Pubkey.new_unique(), b"synthetic", [AccountMeta(other, True, False)])
    tx = Transaction.new_unsigned(Message.new_with_blockhash([ix], signer.PUBLIC_KEY, Hash.new_unique()))
    rpc = Mock()
    monkeypatch.setattr(signer, "_client_for_url", rpc)
    with pytest.raises(ValueError, match="one original"):
        signer.sign_and_send(tx)
    rpc.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected_calls", [("timeout", 3), ("server", 3), ("forbidden", 1), ("bad_json", 1), ("cancel", 1)])
async def test_actual_route_retry_has_only_read_only_http_calls(monkeypatch, failure, expected_calls):
    calls = []
    error = (TimeoutError("synthetic") if failure == "timeout" else
             aiohttp.ClientResponseError(None, (), status=503 if failure == "server" else 403)
             if failure in {"server", "forbidden"} else
             ValueError("synthetic malformed JSON") if failure == "bad_json" else asyncio.CancelledError())

    class Response:
        async def __aenter__(self):
            raise error
        async def __aexit__(self, *args):
            return False

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def get(self, url, **kwargs):
            calls.append(url)
            return Response()

    monkeypatch.setattr(gmgn.aiohttp, "ClientSession", Session)
    route = gmgn._route.retry_with(wait=tenacity.wait_none())
    with pytest.raises(type(error)):
        await route(gmgn.SOL_MINT, "synthetic-mint", 100_000_000, "synthetic-owner")
    assert len(calls) == expected_calls and len(set(calls)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [True, 1.5, 2**64])
async def test_invalid_gmgn_sell_units_never_load_signer_or_route(monkeypatch, quantity):
    route = AsyncMock()
    monkeypatch.setattr(gmgn, "_route", route)
    with pytest.raises(ValueError):
        await gmgn.sell("synthetic-mint", quantity)
    route.assert_not_called()


@pytest.mark.parametrize("operation", ["buy", "sell"])
def test_no_outer_order_retry_decorator(operation):
    assert not hasattr(getattr(gmgn, operation), "retry")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["buy", "sell"])
async def test_real_gmgn_operation_never_requotes_after_send_failure(monkeypatch, operation):
    import trader
    fake = SimpleNamespace(PUBLIC_KEY="synthetic-owner", sign_and_send=Mock(side_effect=TimeoutError("synthetic send")))
    # setattr/getattr would invoke the package's lazy __getattr__ and import
    # the operator signer before installing this isolated test double.
    monkeypatch.setitem(trader.__dict__, "sol_signer", fake)
    route = AsyncMock(return_value={"data": {"raw_tx": {"swapTransaction": "synthetic-packet"}}})
    monkeypatch.setattr(gmgn, "_route", route)
    with pytest.raises(TimeoutError):
        await getattr(gmgn, operation)("synthetic-mint", .1 if operation == "buy" else 2**53 + 1)
    route.assert_awaited_once()
    fake.sign_and_send.assert_called_once()
    assert route.await_args.args[2] == (100_000_000 if operation == "buy" else 2**53 + 1)


@pytest.mark.parametrize("raw", [json.dumps(list(range(32))), base64.b64encode(bytes(range(32))).decode()])
def test_synthetic_key_decoding_keeps_full_json_spaces_and_base64(signer, raw):
    assert signer._decode_secret(raw) == bytes(range(32))


@pytest.mark.parametrize("raw", ["[true]", "[256]", "[-1]", "[1.5]", "[] trailing", "bad key with spaces"])
def test_bad_secret_shapes_do_not_become_keys(signer, raw):
    with pytest.raises((ValueError, TypeError)):
        signer._decode_secret(raw)
