"""Cost-checked paper close labels from frozen entry features, never SQL spot PnL.

This secondary export never submits orders, calls providers, or activates models.
The buy journal and canonical paper close retain its retryable original inputs.
"""
from __future__ import annotations

import copy
import asyncio
import datetime as dt
import hashlib
import json
import math
import time
from pathlib import Path
from collections.abc import Mapping

from features.builder import COLUMNS
from runtime.paper_archive import entry_identity, read_closed_trade, paper_snapshot, _validate_trade
from utils.atomic_json import read_json_strict, write_json_atomic

VERSION = "paper_costed_trade_learning_v1"
ENTRY_VERSION = "frozen_entry_features_v1"
ENTRY_AUX_VERSION = "frozen_entry_features_with_auxiliary_receipts_v2"
ENTRY_ALL_AUX_VERSION = "frozen_entry_features_with_auxiliary_receipts_v3"
ENTRY_STRATEGY_VERSION = "frozen_entry_features_with_strategy_context_v4"
_REPAIR_STATE: dict[str, tuple[float, int]] = {}
FINANCIAL_FIELDS = frozenset("""
entry_intent_id source_position_key buy_signature token_address run_id dry_run closed opened_at closed_at
qty_lamports entry_qty buy_price_usd amount_sol entry_notional_usd execution_cost_model
entry_route_quote quantity_basis price_source_close net_total_pnl_usd net_total_pnl_pct
net_total_pnl_sol total_pnl_usd estimated_fees_usd estimated_fees_sol execution_fill_count
total_proceeds_sol exit_fill_events max_pnl_pct_seen highest_pnl_pct
""".split())


class TradeLearningError(RuntimeError):
    pass


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def _time(value):
    stamp = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp.replace(tzinfo=dt.timezone.utc) if stamp.tzinfo is None else stamp.astimezone(dt.timezone.utc)


def _number(value):
    if isinstance(value, bool): raise TradeLearningError("Boolean is not financial evidence")
    result = float(value)
    if not math.isfinite(result): raise TradeLearningError("Nonfinite financial evidence")
    return result


def freeze_entry_features(vector, *, address, captured_at, positive_pnl_ratio=0., auxiliary_observations=None,
                          strategy_context=None):
    """Whitelist T0 features before the existing durable pre-buy journal write."""
    raw = vector.to_dict() if hasattr(vector, "to_dict") else dict(vector)
    values = {}
    for key in COLUMNS:
        value = raw.get(key)
        if isinstance(value, dt.datetime): value = _time(value).isoformat()
        elif hasattr(value, "item"): value = value.item()
        if isinstance(value, float) and not math.isfinite(value): value = None
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise TradeLearningError("Entry feature is not a scalar")
        values[key] = value
    values["timestamp"] = _time(values["timestamp"]).isoformat()
    payload = {"version": ENTRY_VERSION, "captured_at": _time(captured_at).isoformat(),
        "positive_pnl_ratio": _number(positive_pnl_ratio), "vector": values}
    if auxiliary_observations is not None:
        payload["version"] = (ENTRY_ALL_AUX_VERSION if isinstance(auxiliary_observations, dict)
                              and set(auxiliary_observations) == {"social", "trend", "early_accumulation", "rug", "cluster"}
                              else ENTRY_AUX_VERSION)
        payload["auxiliary_observations"] = copy.deepcopy(auxiliary_observations)
    if strategy_context is not None:
        if payload["version"] != ENTRY_ALL_AUX_VERSION:
            raise TradeLearningError("Strategy context requires complete original auxiliary receipts")
        payload["version"] = ENTRY_STRATEGY_VERSION
        payload["strategy_context"] = copy.deepcopy(strategy_context)
    payload["payload_sha256"] = _hash(payload)
    validate_entry_features(payload, address=address)
    return payload


def validate_entry_features(payload, *, address):
    keys = {"version", "captured_at", "positive_pnl_ratio", "vector", "payload_sha256"}
    if isinstance(payload, dict) and payload.get("version") in {ENTRY_AUX_VERSION, ENTRY_ALL_AUX_VERSION, ENTRY_STRATEGY_VERSION}:
        keys.add("auxiliary_observations")
    if isinstance(payload, dict) and payload.get("version") == ENTRY_STRATEGY_VERSION:
        keys.add("strategy_context")
    if (not isinstance(payload, dict) or set(payload) != keys
            or payload.get("version") not in {ENTRY_VERSION, ENTRY_AUX_VERSION, ENTRY_ALL_AUX_VERSION, ENTRY_STRATEGY_VERSION} or not isinstance(payload.get("vector"), dict)
            or set(payload["vector"]) != set(COLUMNS)
            or payload["vector"].get("address") != address
            or payload["payload_sha256"] != _hash({k: v for k, v in payload.items() if k != "payload_sha256"})
            or _time(payload["vector"]["timestamp"]) > _time(payload["captured_at"])
            or _number(payload["positive_pnl_ratio"]) < 0):
        raise TradeLearningError("Invalid frozen entry feature proof")
    if payload["version"] == ENTRY_STRATEGY_VERSION:
        from features.strategy_context import validate_strategy_context
        try:
            validate_strategy_context(payload["strategy_context"])
        except ValueError as exc:
            raise TradeLearningError("Invalid frozen strategy context") from exc
    if payload["version"] in {ENTRY_AUX_VERSION, ENTRY_ALL_AUX_VERSION, ENTRY_STRATEGY_VERSION}:
        from runtime.entry_observation import social_observation_problem, auxiliary_observation_problem
        from utils.auxiliary_observation import KINDS, clock_after
        observations = payload["auxiliary_observations"]
        expected = {"social"} | (KINDS if payload["version"] in {ENTRY_ALL_AUX_VERSION, ENTRY_STRATEGY_VERSION} else set())
        if not isinstance(observations, dict) or set(observations) != expected:
            raise TradeLearningError("Unsupported frozen auxiliary observation schema")
        social = observations["social"]
        captured = _time(payload["vector"]["timestamp"]).timestamp()
        problem = social_observation_problem(social, address=address, vector=payload["vector"], now=captured)
        if (problem is not None or any(social.get(field) is not None
                                     and clock_after(social[field], captured)
                                     for field in ("received_at", "risk_checked_at"))):
            raise TradeLearningError("Invalid or noncausal frozen social observation")
        if payload["version"] in {ENTRY_ALL_AUX_VERSION, ENTRY_STRATEGY_VERSION}:
            problem = auxiliary_observation_problem({k: observations[k] for k in KINDS},
                address=address, vector=payload["vector"], now=captured, causal=True)
            if problem is not None:
                raise TradeLearningError("Invalid or noncausal frozen auxiliary observation")


def validate_source(source):
    """Recheck proof on export and on model reads; never fall back to gross."""
    from analytics.forward_evidence import _costed_close
    keys = {"version", "trade_id", "entry_features", "buy_proof", "trade", "payload_sha256"}
    if isinstance(source, dict) and "entry_decision" in source:
        keys.add("entry_decision")
    if (not isinstance(source, dict) or set(source) != keys
            or source.get("version") != VERSION
            or source["payload_sha256"] != _hash({k: v for k, v in source.items() if k != "payload_sha256"})):
        raise TradeLearningError("Corrupt costed close source")
    trade, buy = source["trade"], source["buy_proof"]
    identity = _validate_trade(trade)
    if (identity != source["trade_id"] or entry_identity(trade) != identity or trade.get("dry_run") is not True
            or trade.get("buy_signature") != "SIM-" + identity
            or not isinstance(buy, dict) or set(buy) != {"intent_id", "address", "run_id", "amount_sol", "fill"}
            or buy["intent_id"] != identity or buy["address"] != trade["token_address"]
            or not buy["run_id"] or buy["run_id"] != trade.get("run_id")
            or buy["amount_sol"] != .1 or trade.get("amount_sol") != .1
            or any(type(trade.get(key)) is not int for key in ("qty_lamports", "entry_qty", "execution_fill_count"))):
        raise TradeLearningError("Close source identity/quantity conflicts")
    fill = buy["fill"]
    quote = trade.get("entry_route_quote")
    from execution.quote_receipt import valid_summary
    from fetcher.jupiter_router import SOL_MINT
    if (not isinstance(fill, dict) or type(fill.get("qty_lamports")) is not int
            or not 0 < trade["entry_qty"] <= 2**63 - 1 or not isinstance(quote, dict)
            or not valid_summary(quote, input_mint=SOL_MINT, output_mint=trade["token_address"],
                amount=100000000, not_after=_time(trade["opened_at"]), allow_legacy=True)
            or _time(trade["closed_at"]) > dt.datetime.now(dt.timezone.utc)):
        raise TradeLearningError("Unconfirmed raw quote quantity or future close")
    for name, target in (("qty_lamports", "entry_qty"), ("buy_price_usd", "buy_price_usd"),
            ("entry_notional_usd", "entry_notional_usd"), ("signature", "buy_signature")):
        if fill.get(name) != trade.get(target): raise TradeLearningError("Buy and close lineage conflicts")
    validate_entry_features(source["entry_features"], address=trade["token_address"])
    if "entry_decision" in source:
        from runtime.entry_decision import validate_entry_decision
        validate_entry_decision(source["entry_decision"], entry_features=source["entry_features"],
            intent_id=identity, run_id=trade["run_id"], paper=True, amount_sol=buy["amount_sol"])
    if _time(source["entry_features"]["captured_at"]) > _time(trade["opened_at"]):
        raise TradeLearningError("Entry features were not captured before the fill")
    costed = _costed_close(trade)
    if costed is None or costed[-1] is not True:
        raise TradeLearningError("No complete executable estimated-net close")
    remaining, previous, proceeds, seen = trade["entry_qty"], _time(trade["opened_at"]), 0., set()
    events = trade.get("exit_fill_events")
    if not isinstance(events, list) or len(events) != trade["execution_fill_count"] - 1:
        raise TradeLearningError("Incomplete exit receipt population")
    for event in events:
        response, exit_id = event["response"], event["intent_id"]
        stamp, qty = _time(response["filled_at"]), response["qty_sold"]
        if (not isinstance(exit_id, str) or len(exit_id) != 32 or any(c not in "0123456789abcdef" for c in exit_id)
                or exit_id in seen or response.get("exit_intent_id") != exit_id
                or response.get("signature") != "SIM-EXIT-" + exit_id or response.get("ok") is not True
                or response.get("venue") != "paper" or response.get("price_source_close") != "jupiter_reverse_quote"
                or type(event.get("qty_before")) is not int or event["qty_before"] != remaining
                or type(qty) is not int or not 0 < qty <= remaining
                or type(response.get("qty_left")) is not int or response["qty_left"] != remaining - qty
                or response.get("partial") is not (qty < remaining) or not previous <= stamp <= _time(trade["closed_at"])):
            raise TradeLearningError("Exit receipt lineage/quantity/time conflicts")
        price = _number(response["price_used_usd"])
        if price <= 0: raise TradeLearningError("Invalid exit receipt price")
        fill_proceeds = qty / trade["entry_qty"] * trade["entry_notional_usd"] / trade["buy_price_usd"] * price
        route = response.get("exit_route_quote")
        if route is not None or "observation_receipt" in quote:
            if (not valid_summary(route, input_mint=trade["token_address"], output_mint=SOL_MINT,
                    amount=qty, not_after=stamp) or route["max_impact_pct"] != quote["max_impact_pct"]):
                raise TradeLearningError("Exit observation-only quote differs from paper fill")
            sol_usd = _number(response.get("quote_sol_usd"))
            expected = route["out_amount"] / 1e9 * sol_usd * (1 - trade["execution_cost_model"]["slippage_bps"] / 10000)
            if sol_usd <= 0 or not math.isclose(fill_proceeds, expected, rel_tol=1e-7, abs_tol=1e-8):
                raise TradeLearningError("Exit quote valuation does not conserve paper proceeds")
        proceeds += fill_proceeds
        remaining, previous = remaining - qty, stamp
        seen.add(exit_id)
    if (remaining != 0 or previous != _time(trade["closed_at"])
            or not math.isclose(proceeds - trade["entry_notional_usd"], trade["total_pnl_usd"], rel_tol=1e-7, abs_tol=1e-8)):
        raise TradeLearningError("Terminal receipts do not reconcile gross proceeds")
    return costed[1]


def _directory(root):
    return Path(root) / "data" / "learning" / "trade_closes"


def _capture(identity, *, root):
    from runtime.buy_recovery import BuyRecoveryStore
    root = Path(root)
    journals = [root / "data" / "metrics" / "buy_recovery" / sub / (identity + ".json") for sub in ("", "resolved")]
    available = [path for path in journals if path.exists()]
    if len(available) != 1: raise TradeLearningError("Missing or contradictory pre-buy journal")
    journal = read_json_strict(available[0])
    BuyRecoveryStore._validate_row(journal)
    if journal["state"] != "persisted" or journal["paper"] is not True or "entry_features" not in journal:
        raise TradeLearningError("No acknowledged frozen pre-buy feature source")
    archive = root / "data" / "paper_closed_trades" / (identity + ".json")
    trade = read_closed_trade(archive) if archive.exists() else None
    primary = root / "data" / "paper_portfolio.json"
    try: portfolio = read_json_strict(primary)
    except FileNotFoundError: portfolio = {}
    if not isinstance(portfolio, dict): raise TradeLearningError("Unreadable primary portfolio")
    current = portfolio.get(journal["address"])
    if isinstance(current, dict) and entry_identity(current) == identity:
        snapshot = paper_snapshot(current, journal["address"])
        _validate_trade(snapshot)
        if trade is not None and any(trade.get(key) != snapshot.get(key) for key in FINANCIAL_FIELDS):
            raise TradeLearningError("Contradictory primary and archived financial close")
        if trade is None: trade = snapshot
    if trade is None: raise TradeLearningError("Original financial close is unavailable")
    source = {"version": VERSION, "trade_id": identity, "entry_features": copy.deepcopy(journal["entry_features"]),
        "buy_proof": {"intent_id": journal["intent_id"], "address": journal["address"],
            "run_id": journal["base_position"].get("run_id"), "amount_sol": journal["amount_sol"],
            "fill": {k: journal["fill"].get(k) for k in ("qty_lamports", "buy_price_usd", "entry_notional_usd", "signature")}},
        "trade": {k: copy.deepcopy(v) for k, v in trade.items() if k in FINANCIAL_FIELDS}}
    if "entry_decision" in journal:
        source["entry_decision"] = copy.deepcopy(journal["entry_decision"])
    source["payload_sha256"] = _hash(source)
    validate_source(source)
    return source


def checked_position_outcome(position, *, root):
    """Unknown/live/open/legacy closes are not fabricated financial outcomes."""
    if (getattr(position, "dry_run", False) is not True or getattr(position, "closed", False) is not True
            or type(getattr(position, "qty", None)) is not int or position.qty != 0):
        return None
    identity = entry_identity({"source_position_key": getattr(position, "source_position_key", None)})
    if identity is None: return None
    source = prepare_close(identity, root=root)
    if source["trade"]["token_address"] != position.address or source["trade"]["run_id"] != position.run_id:
        raise TradeLearningError("SQL position and financial source identity conflict")
    net = validate_source(source)
    return "win" if net / 100 >= source["entry_features"]["positive_pnl_ratio"] else "fail"


def prepare_close(identity, *, root):
    if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        raise TradeLearningError("Invalid causal learning identity")
    path = _directory(root) / (identity + ".json")
    source = read_json_strict(path) if path.exists() else _capture(identity, root=root)
    validate_source(source)
    if source["trade_id"] != identity: raise TradeLearningError("Learning filename/identity conflicts")
    if not path.exists(): write_json_atomic(path, source)
    return source


def publish_close(identity, *, root):
    from features import store
    source = prepare_close(identity, root=root)
    net = validate_source(source)
    entry, trade = source["entry_features"], source["trade"]
    vector = {**entry["vector"], "timestamp": _time(entry["vector"]["timestamp"])}
    from features.auxiliary_semantics import PROOF_COLUMN
    vector[PROOF_COLUMN] = json.dumps(entry, sort_keys=True, separators=(",", ":"), allow_nan=False)
    written = store.append(vector, int(net / 100 >= entry["positive_pnl_ratio"]),
        target_total_pnl_pct=net, sample_type="trade_close", strict=True,
        partition_at=_time(trade["closed_at"]), outcome_targets={
            "max_pnl_pct_seen": trade.get("max_pnl_pct_seen", trade.get("highest_pnl_pct")),
            "outcome_closed_at": _time(trade["closed_at"]), "outcome_trade_id": identity,
            "outcome_source_sha256": source["payload_sha256"], "outcome_return_basis": VERSION,
            "outcome_gross_pnl_pct": 100 * trade["total_pnl_usd"] / trade["entry_notional_usd"],
            "outcome_execution_proof": json.dumps(source, sort_keys=True, allow_nan=False)})
    return {"status": "written" if written else "already_written", "feature_timestamp": entry["vector"]["timestamp"]}


def repair_exports(*, root, cfg, force=False, limit=8):
    if getattr(cfg, "DRY_RUN", False) is not True:
        return {"status": "disabled", "attempted": 0, "failed": 0}
    root, now = Path(root).resolve(), time.monotonic()
    key = str(root)
    previous, cursor = _REPAIR_STATE.get(key, (0., 0))
    if not force and now - previous < 30: return {"status": "throttled", "attempted": 0, "failed": 0}
    if len(_REPAIR_STATE) >= 32 and key not in _REPAIR_STATE: _REPAIR_STATE.pop(next(iter(_REPAIR_STATE)))
    try:
        candidates = set()
        for directory in (_directory(root), root / "data" / "paper_closed_trades"):
            try: candidates.update(path.stem for path in directory.iterdir() if path.name.endswith(".json"))
            except FileNotFoundError: pass
        try: portfolio = read_json_strict(root / "data" / "paper_portfolio.json")
        except FileNotFoundError: portfolio = {}
        if not isinstance(portfolio, dict): raise TradeLearningError("Unreadable primary portfolio")
        for entry in portfolio.values():
            if isinstance(entry, dict) and entry.get("closed") is True and (identity := entry_identity(entry)):
                candidates.add(identity)
        candidates = sorted(candidates)
    except (OSError, ValueError, TypeError, RuntimeError):
        _REPAIR_STATE[key] = (now, cursor)
        return {"status": "pending", "attempted": 0, "failed": 1, "source_scan_failed": True}
    attempted = failed = written = 0
    while candidates and attempted < min(max(1, min(8, int(limit))), len(candidates)):
        identity = candidates[cursor % len(candidates)]
        cursor += 1
        attempted += 1
        try:
            result = publish_close(identity, root=root)
            written += result["status"] == "written"
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError): failed += 1
    _REPAIR_STATE[key] = (now, cursor % len(candidates) if candidates else 0)
    return {"status": "pending" if failed else "ok", "attempted": attempted, "failed": failed, "written": written}


async def run_export_loop(*, ready, root, cfg, on_result, on_error, sleep=asyncio.sleep):
    """One supervised secondary owner; slow Parquet I/O cannot block exits.

    Cancellation drains the owned filesystem worker before runtime shutdown is
    published. No overlapping exports or abandoned to_thread jobs are started.
    """
    await ready.wait()
    while True:
        worker = asyncio.create_task(asyncio.to_thread(repair_exports, root=root, cfg=cfg, force=True))
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            await asyncio.gather(worker, return_exceptions=True)
            raise
        except Exception as exc:
            on_error(exc)
        else:
            on_result(result)
        await sleep(30)
