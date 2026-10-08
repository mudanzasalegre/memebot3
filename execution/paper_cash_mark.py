"""Exact-quantity PAPER cash marks, never physical token prices or live fills.

The private policy price is normalized to the frozen entry reference so the
existing exit engine can consume a cash return without confusing raw SPL units
with human token units. Quote cash already includes the frozen adverse PAPER
slippage; fees are reported separately. No market timestamp is manufactured.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import SimpleNamespace
from typing import Any, Mapping

from execution.quote_receipt import capture_summary, valid_summary
from utils.raw_units import U64_MAX, sol_to_lamports
from utils.sol_price import SolUsdObservation, fresh_sol_usd

SOL_MINT = "So11111111111111111111111111111111111111112"
VERSION = "paper_exact_cash_mark_v1"


def _number(value: Any, *, positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Unknown typed money basis")
    value = float(value)
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError("Invalid money basis")
    return value


def _quantity(value: Any, *, positive=False) -> int:
    if type(value) is not int or not (1 if positive else 0) <= value <= U64_MAX:
        raise ValueError("Unknown raw quantity")
    return value


def _time(value: Any) -> dt.datetime:
    stamp = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Unknown observation timezone")
    return stamp.astimezone(dt.timezone.utc)


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def basis(entry: Mapping, *, token: str, owner: str) -> dict:
    """Freeze financial identity, not mutable peaks or physical market data."""
    from utils.solana_addr import is_valid_base58_32
    if (not isinstance(entry, Mapping) or entry.get("dry_run") is not True
            or entry.get("closed") is not False or not is_valid_base58_32(token)
            or re.fullmatch(r"(?:buy:[0-9a-f]{32}|case:[0-9a-f]{64})", owner or "") is None
            or entry.get("quantity_basis") != "quoted_raw_spl_units"):
        raise ValueError("Unknown quoted PAPER identity")
    if entry.get("token_address") not in (None, token):
        raise ValueError("Conflicting PAPER mint")
    if owner.startswith("buy:"):
        from runtime.paper_archive import entry_identity
        if entry_identity(entry) != owner[4:] or entry.get("buy_signature") != "SIM-" + owner[4:]:
            raise ValueError("Conflicting original PAPER buy")
    entry_qty = _quantity(entry.get("entry_qty"), positive=True)
    remaining = _quantity(entry.get("qty_lamports"), positive=True)
    realized = _quantity(entry.get("realized_qty"))
    if entry_qty != remaining + realized:
        raise ValueError("PAPER quantity conservation failed")
    opened = _time(entry.get("opened_at"))
    model = entry.get("execution_cost_model")
    if (not isinstance(model, Mapping) or model.get("version") != "estimated-v1"
            or model.get("observed_execution") is not False):
        raise ValueError("Unknown PAPER cost model")
    slip = _number(model.get("slippage_bps"))
    fee = _number(model.get("fee_sol_per_fill"))
    if slip >= 10000:
        raise ValueError("Invalid PAPER slippage")
    amount = _number(entry.get("amount_sol"), positive=True)
    route = entry.get("entry_route_quote")
    if not valid_summary(route, input_mint=SOL_MINT, output_mint=token,
            amount=sol_to_lamports(amount), not_after=opened):
        raise ValueError("Unknown original entry quote")
    if entry_qty != int(route["out_amount"] / (1 + slip / 10000)):
        raise ValueError("Entry quantity differs from frozen original quote")
    return {"token": token, "owner": owner, "opened_at": opened.isoformat(),
        "run_id": entry.get("run_id"), "entry_qty": entry_qty, "remaining_qty": remaining,
        "realized_qty": realized, "amount_sol": amount,
        "entry_notional_usd": _number(entry.get("entry_notional_usd"), positive=True),
        "entry_reference_price_usd": _number(entry.get("buy_price_usd"), positive=True),
        "realized_proceeds_usd": _number(entry.get("realized_proceeds_usd")),
        "estimated_fees_usd": _number(entry.get("estimated_fees_usd")),
        "cost_model": {"version": "estimated-v1", "observed_execution": False,
            "slippage_bps": slip, "fee_sol_per_fill": fee},
        "entry_quote_sha256": route["observation_receipt"]["sha256"]}


@dataclass(frozen=True)
class PaperCashMark:
    receipt_json: str

    def to_dict(self) -> dict:
        return json.loads(self.receipt_json)


def capture(entry: Mapping, quote: Any, fx: SolUsdObservation, *, token: str,
            owner: str, now: dt.datetime, slippage_bps: int) -> PaperCashMark:
    frozen = basis(entry, token=token, owner=owner)
    now = _time(now)
    if now < _time(frozen["opened_at"]):
        raise ValueError("Mark predates the entry")
    rate = fresh_sol_usd(fx, now=now.timestamp())
    if rate is None:
        raise ValueError("Unknown or expired original FX observation")
    route = capture_summary(quote, input_mint=token, output_mint=SOL_MINT,
        amount=frozen["remaining_qty"], slippage=slippage_bps,
        limit=entry["entry_route_quote"]["max_impact_pct"], now=now)
    model = frozen["cost_model"]
    cash_sol = route["out_amount"] / 1e9 * (1 - model["slippage_bps"] / 10000)
    cash_usd = cash_sol * rate
    remaining_cost = frozen["entry_notional_usd"] * frozen["remaining_qty"] / frozen["entry_qty"]
    fee_usd = model["fee_sol_per_fill"] * rate
    policy_price = frozen["entry_reference_price_usd"] * cash_usd / remaining_cost
    values = {"remaining_cost_usd": remaining_cost, "quoted_proceeds_sol": cash_sol,
        "quoted_proceeds_usd": cash_usd, "estimated_exit_fee_usd": fee_usd,
        "policy_reference_price_usd": policy_price,
        "gross_remaining_return_pct": (cash_usd / remaining_cost - 1) * 100,
        "estimated_remaining_net_return_pct": ((cash_usd - fee_usd) / remaining_cost - 1) * 100,
        "estimated_total_liquidation_net_pnl_usd": frozen["realized_proceeds_usd"] + cash_usd
            - frozen["entry_notional_usd"] - frozen["estimated_fees_usd"] - fee_usd}
    if (not all(math.isfinite(v) for v in values.values()) or remaining_cost <= 0
            or cash_sol <= 0 or cash_usd <= 0 or policy_price <= 0):
        raise ValueError("Nonfinite or empty cash mark")
    body = {"version": VERSION, "role": "estimated_exit_valuation_only",
        "observed_execution": False, "physical_token_price": False,
        "valued_at": now.isoformat(), "requested_slippage_bps": slippage_bps,
        "basis": frozen, "route_quote": route,
        "fx_observation": fx.to_dict(), "values": values}
    body["sha256"] = _digest(body)
    return PaperCashMark(_json(body))


def checked_price(mark: Any, entry: Mapping, *, token: str, owner: str,
                  now: dt.datetime) -> float | None:
    """Recompute original money, exact state and age; never renew a receipt."""
    try:
        row = mark.to_dict() if isinstance(mark, PaperCashMark) else mark
        if not isinstance(row, dict) or row.get("sha256") != _digest({k: v for k, v in row.items() if k != "sha256"}):
            return None
        valued = _time(row["valued_at"])
        now = _time(now)
        if not 0 <= (now - valued).total_seconds() <= 10:
            return None
        public = row["route_quote"]["observation_receipt"]
        quote = SimpleNamespace(ok=True, raw=public["raw"], other=public["other"],
            in_amount=public["in_amount"], out_amount=public["out_amount"],
            price_impact_bps=public["price_impact_bps"])
        fx = SolUsdObservation(**row["fx_observation"])
        original = capture(entry, quote, fx, token=token, owner=owner, now=valued,
                           slippage_bps=row["requested_slippage_bps"]).to_dict()
        if row != original:
            return None
        current = capture(entry, quote, fx, token=token, owner=owner, now=now,
                          slippage_bps=row["requested_slippage_bps"])
        return current.to_dict()["values"]["policy_reference_price_usd"]
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError, RecursionError):
        return None


def matches_sql(entry: Mapping, position: Any, *, token: str) -> bool:
    """Require the same original buy and current money/quantity generation.

    The SQL admission timestamp and portfolio fill timestamp are distinct;
    equality of those two clocks is deliberately not inferred.
    """
    try:
        from runtime.paper_archive import entry_identity
        identity = entry_identity(entry)
        read = position.get if isinstance(position, Mapping) else lambda key, default=None: getattr(position, key, default)
        if (identity is None or read("source_position_key") != "buy:" + identity
                or read("buy_tx_sig") != entry.get("buy_signature")
                or read("dry_run") is not True or read("closed") is not False
                or (read("token_mint") or read("address")) != token):
            return False
        for source, target in (("entry_qty", "entry_qty"), ("qty_lamports", "qty"), ("realized_qty", "realized_qty")):
            if _quantity(entry.get(source)) != _quantity(read(target)):
                return False
        for source, target in (("amount_sol", "buy_amount_sol"), ("buy_price_usd", "buy_price_usd"),
                ("entry_notional_usd", "entry_notional_usd"), ("realized_proceeds_usd", "realized_proceeds_usd")):
            if _number(entry.get(source)) != _number(read(target)):
                return False
        return True
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return False


def public_historical_mark(mark: Any, entry: Mapping, *, token: str, owner: str) -> dict:
    """Preserve a checked original mark after later fills, without renewing it.

    This is historical estimated valuation, not current route/fill acceptance.
    Only the recorded quantity/cash generation is reconstructed; the original
    buy identity, cost model, notional and entry quote must still agree.
    """
    row = mark.to_dict() if isinstance(mark, PaperCashMark) else mark
    if not isinstance(row, dict) or not isinstance(row.get("basis"), dict):
        raise ValueError("Missing historical cash basis")
    frozen = row["basis"]
    valued = _time(row.get("valued_at"))
    if type(entry.get("closed")) is not bool:
        raise ValueError("Unknown historical position state")
    if entry["closed"] and valued > _time(entry.get("closed_at")):
        raise ValueError("Cash mark follows the terminal fill")
    if (_quantity(entry.get("qty_lamports")) > _quantity(frozen.get("remaining_qty"), positive=True)
            or _quantity(entry.get("realized_qty")) < _quantity(frozen.get("realized_qty"))
            or _number(entry.get("realized_proceeds_usd")) < _number(frozen.get("realized_proceeds_usd"))
            or _number(entry.get("estimated_fees_usd")) < _number(frozen.get("estimated_fees_usd"))):
        raise ValueError("Cash mark is from a later financial generation")
    original = dict(entry, closed=False, qty_lamports=frozen["remaining_qty"],
        realized_qty=frozen["realized_qty"], realized_proceeds_usd=frozen["realized_proceeds_usd"],
        estimated_fees_usd=frozen["estimated_fees_usd"])
    if checked_price(row, original, token=token, owner=owner, now=valued) is None:
        raise ValueError("Historical cash receipt differs from its original basis")
    return json.loads(_json(row))
