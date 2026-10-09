from __future__ import annotations

import datetime as dt
import json
from hashlib import sha256

import pytest

from analytics.forward_evidence import collect_forward_evidence, forward_acceptance


def _row(*, address="CaseSensitiveMint", opened=None, pct=10):
    now = dt.datetime.now(dt.timezone.utc)
    opened = opened or now - dt.timedelta(hours=26)
    import pandas as pd
    from net_financial_fixtures import net_frame
    frame = net_frame(pd.DataFrame([dict(address=address, timestamp=opened,
        ts=opened + dt.timedelta(hours=1), target_total_pnl_pct=pct)]))
    trade = json.loads(frame.iloc[0].outcome_execution_proof)["trade"]
    identity = sha256((trade["token_address"] + opened.isoformat()).encode()).hexdigest()[:32]
    trade.update(entry_intent_id=identity, buy_signature="SIM-"+identity,
                 run_id="trial", config_profile="challenger", config_hash="hash", highest_pnl_pct=5000)
    return trade


def _collect(tmp_path, rows, *, started=None):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "paper_closed_trades.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    return collect_forward_evidence(tmp_path, run_id="trial", profile="challenger", config_hash="hash",
                                    started_at=started or dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48))


@pytest.mark.parametrize("changes", [
    {"execution_cost_model": {"version": "estimated-v1"}},
    {"execution_cost_model": {"version": "estimated-v1", "slippage_bps": 100, "fee_sol_per_fill": .000025, "observed_execution": True}},
    {"net_total_pnl_usd": 999}, {"net_total_pnl_pct": 999}, {"estimated_fees_sol": 0},
    {"estimated_fees_usd": 0}, {"execution_fill_count": float("inf")},
    {"entry_notional_usd": 0}, {"net_total_pnl_sol": float("nan")},
    {"total_proceeds_sol": 999}, {"qty_lamports": 1},
])
def test_cost_label_or_inconsistent_accounting_is_not_costed_evidence(tmp_path, changes):
    row = {**_row(), **changes}
    evidence = _collect(tmp_path, [row, dict(row)])
    assert evidence["closed_trades"] == 0
    assert evidence["uncosted_records"] == 1
    assert "unknown_execution_costs" in forward_acceptance(evidence)["rejection_reasons"]


def test_terminal_close_supersedes_stale_partial_and_duplicate_snapshots(tmp_path):
    row = _row()
    stale = {**row, "closed": False, "qty_lamports": row["entry_qty"], "closed_at": None}
    evidence = _collect(tmp_path, [stale, row, row, stale])
    assert evidence["closed_trades"] == 1 and evidence["open_positions"] == 0
    assert evidence["quote_backed_closed_trades"] == 1
    assert evidence["total_pnl_usd"] == pytest.approx(1)
    assert evidence["runner_capture_ratio"] == pytest.approx(.002)  # 10% net, NOT a +5,000% gain


def test_conflicting_costed_copies_are_not_arbitrarily_selected(tmp_path):
    first = _row(pct=10)
    second = _row(pct=20, opened=dt.datetime.fromisoformat(first["opened_at"]))
    evidence = _collect(tmp_path, [first, second])
    assert evidence["closed_trades"] == 0
    assert "conflicting_costed_trade_records" in evidence["evidence_rejections"]


def test_case_sensitive_mints_and_repeat_entries_are_distinct(tmp_path):
    opened = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=26)
    rows = [_row(address="Mint", opened=opened), _row(address="mint", opened=opened),
            _row(address="Mint", opened=opened + dt.timedelta(hours=2))]
    evidence = _collect(tmp_path, rows)
    assert evidence["closed_trades"] == 3
    assert evidence["distinct_closed_tokens"] == 2
    assert evidence["committed_capital_sol"] == pytest.approx(.3)
    assert evidence["net_return_on_committed_capital_pct"] == pytest.approx(10)


@pytest.mark.parametrize("changes", [{"dry_run": "false"}, {"dry_run": 2}, {"test_event": True},
                                      {"run_id": "other"}, {"config_profile": "other"}, {"config_hash": "other"}])
def test_live_test_or_other_cohort_rows_are_excluded(tmp_path, changes):
    assert _collect(tmp_path, [{**_row(), **changes}])["closed_trades"] == 0


def test_synthetic_prices_are_separate_from_exact_quote_evidence(tmp_path):
    row = _row()
    from execution.paper_execution_cost import ENTRY_FIELDS as COST_FIELDS
    from execution.paper_execution_fx import ENTRY_FIELDS as FX_FIELDS, EXIT_FIELDS
    for name in (*COST_FIELDS, *FX_FIELDS): row.pop(name)
    for event in row["exit_fill_events"]:
        for name in EXIT_FIELDS[:2]: event["response"].pop(name)
    row.update(entry_route_quote=None, quantity_basis="synthetic_paper_units", price_source_close="price_api")
    evidence = _collect(tmp_path, [row])
    assert evidence["closed_trades"] == 1 and evidence["quote_backed_closed_trades"] == 0
    assert "paper_fills_without_exact_quote_evidence" in forward_acceptance(evidence)["rejection_reasons"]


def test_fresh_cost_checked_positive_cohort_passes_only_paper_diagnostics(tmp_path):
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
    rows = [_row(address=f"Mint{i}", opened=start + dt.timedelta(minutes=30 * i)) for i in range(50)]
    evidence = _collect(tmp_path, rows, started=start)
    assert evidence["elapsed_hours"] >= 24
    assert evidence["quote_backed_closed_trades"] == 50
    acceptance = forward_acceptance(evidence)
    assert acceptance["passed"] is True
    assert "manual approval" in " ".join(acceptance["limitations"])
    again = _collect(tmp_path, rows, started=start)
    assert evidence["token_cluster_mean_return_lower_bootstrap_diagnostic_pct"] == again["token_cluster_mean_return_lower_bootstrap_diagnostic_pct"]


def test_many_repeats_of_one_token_are_not_fifty_independent_tokens(tmp_path):
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
    rows = [_row(opened=start + dt.timedelta(minutes=30 * i)) for i in range(50)]
    evidence = _collect(tmp_path, rows, started=start)
    assert evidence["closed_trades"] == 50 and evidence["distinct_closed_tokens"] == 1
    assert "token_cluster_expectancy_not_established" in forward_acceptance(evidence)["rejection_reasons"]


def test_future_window_cannot_supply_forward_evidence(tmp_path):
    evidence = _collect(tmp_path, [_row()], started=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
    assert "prospective_start_in_future" in evidence["evidence_rejections"]
    assert evidence["closed_trades"] == 0


def test_observed_window_is_not_time_elapsed_while_bot_was_stopped(tmp_path):
    row = _row(opened=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2))
    evidence = _collect(tmp_path, [row], started=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30))
    assert evidence["elapsed_hours"] == pytest.approx(1)


def test_missing_trade_identity_blocks_acceptance_without_hiding_later_valid_records(tmp_path):
    row = _row()
    evidence = _collect(tmp_path, [{**row, "token_address": ""}, row])
    assert "forward_trade_identity_missing" in evidence["evidence_rejections"]
    assert evidence["closed_trades"] == 1
