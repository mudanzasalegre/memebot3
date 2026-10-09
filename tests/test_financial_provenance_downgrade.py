"""Original financial proof cannot downgrade to legacy/scalar compatibility."""
import copy
import json

import pandas as pd
import pytest

from execution.paper_execution_fx import ENTRY_FIELDS, EXIT_FIELDS
from runtime import trade_learning as learning
from test_paper_archive import paper
from test_trade_learning import closed, clean_learning


def remove_original_fx(source):
    source = copy.deepcopy(source)
    for row in (source["trade"], source["buy_proof"]["fill"]):
        for name in ENTRY_FIELDS: row.pop(name, None)
    for event in source["trade"]["exit_fill_events"]:
        for name in EXIT_FIELDS[:2]: event["response"].pop(name, None)
    source["payload_sha256"] = learning._hash({k: v for k, v in source.items() if k != "payload_sha256"})
    return source


def training_row(source):
    return {**source["entry_features"]["vector"], "sample_type": "trade_close",
        "outcome_return_basis": learning.VERSION, "outcome_trade_id": source["trade_id"],
        "outcome_source_sha256": source["payload_sha256"], "outcome_execution_proof": json.dumps(source),
        "outcome_closed_at": source["trade"]["closed_at"], "target_total_pnl_pct": source["trade"]["net_total_pnl_pct"]}


@pytest.mark.asyncio
async def test_financial_learning_cannot_downgrade_original_costed_source_to_scalar_fx(closed):
    source = learning.prepare_close(closed.identity, root=closed.root)
    assert learning.validate_source(source) == pytest.approx(-3.)
    stripped = remove_original_fx(source)
    with pytest.raises(learning.TradeLearningError):
        learning.validate_source(stripped)


@pytest.mark.asyncio
async def test_direct_financial_forward_consumer_rejects_original_cost_without_original_fx(closed):
    from analytics.forward_evidence import _costed_close
    source = remove_original_fx(learning.prepare_close(closed.identity, root=closed.root))
    value = _costed_close(source["trade"])
    assert value is None or value[-1] is False


@pytest.mark.asyncio
async def test_net_training_cannot_certify_a_rehashed_scalar_only_financial_source(closed):
    from ml.financial_targets import checked_financial_frame
    source = remove_original_fx(learning.prepare_close(closed.identity, root=closed.root))
    row = training_row(source)
    checked, report = checked_financial_frame(pd.DataFrame([row]))
    assert checked.empty and not report["ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["all_fx", "all_cost", "all_original", "entry_version",
    "entry_observation", "entry_clock", "entry_null", "buy_fx_only", "exit_fx_only",
    "entry_quote_receipt", "entry_quote_none", "exit_quote_none", "exit_scalar",
    "unknown_fx_version"])
async def test_rehashing_cannot_replace_missing_original_financial_receipts(closed, fault):
    from analytics.forward_evidence import _costed_close
    from execution.paper_execution_cost import ENTRY_FIELDS as COST_FIELDS
    from ml.financial_targets import checked_financial_frame, apply_checked_net_returns
    source = learning.prepare_close(closed.identity, root=closed.root)
    trade, fill = source["trade"], source["buy_proof"]["fill"]
    if fault in {"all_fx", "all_original"}:
        source = remove_original_fx(source)
        trade, fill = source["trade"], source["buy_proof"]["fill"]
    if fault in {"all_cost", "all_original"}:
        for obj in (trade, fill):
            for name in COST_FIELDS: obj.pop(name)
    if fault in {"entry_version", "entry_observation", "entry_clock"}:
        name = {"entry_version": ENTRY_FIELDS[0], "entry_observation": ENTRY_FIELDS[1],
                "entry_clock": ENTRY_FIELDS[2]}[fault]
        for obj in (trade, fill): obj.pop(name)
    elif fault == "entry_null":
        for obj in (trade, fill):
            for name in ENTRY_FIELDS: obj[name] = None
    elif fault == "buy_fx_only":
        for name in ENTRY_FIELDS: fill.pop(name)
    elif fault == "exit_fx_only":
        for event in trade["exit_fill_events"]:
            for name in EXIT_FIELDS[:2]: event["response"].pop(name)
    elif fault == "entry_quote_receipt": trade["entry_route_quote"].pop("observation_receipt")
    elif fault == "entry_quote_none": trade["entry_route_quote"] = None
    elif fault == "exit_quote_none": trade["exit_fill_events"][0]["response"]["exit_route_quote"] = None
    elif fault == "exit_scalar": trade["exit_fill_events"][0]["response"]["quote_sol_usd"] = 0.
    elif fault == "unknown_fx_version":
        for obj in (trade, fill): obj[ENTRY_FIELDS[0]] = "unknown"
    source["payload_sha256"] = learning._hash({k: v for k, v in source.items() if k != "payload_sha256"})
    with pytest.raises(learning.TradeLearningError): learning.validate_source(source)
    # A missing original buy journal can only be checked by the learning
    # consumer; a standalone close has no access to that external journal.
    if fault != "buy_fx_only":
        costed = _costed_close(trade)
        assert costed is None or costed[-1] is False
    row = training_row(source)
    row.update(total_pnl_pct=5000., realized_pnl_pct=5000., pnl_pct=5000., label=1, max_pnl_pct_seen=50000.)
    frame = pd.DataFrame([row])
    checked, report = checked_financial_frame(frame)
    assert checked.empty and not report["ready"]
    restored = apply_checked_net_returns(frame)
    assert restored[["target_total_pnl_pct", "total_pnl_pct", "realized_pnl_pct", "pnl_pct", "label"]].isna().all().all()
    assert restored.max_pnl_pct_seen.isna().all()  # No gross/peak fallback for declared broken proof.


@pytest.mark.parametrize("net", [-150., -3., 500., 1000., 5000., 50000.])
def test_complete_original_receipts_keep_signed_returns_and_uncapped_extreme_targets(net):
    from analytics.forward_evidence import _costed_close
    from ml.financial_targets import checked_financial_frame, supported_financial_training
    from net_financial_fixtures import net_frame
    raw = pd.DataFrame([dict(address="synthetic-extreme-mint", timestamp="2026-09-01T00:00:00Z",
                            ts="2026-09-01T00:02:00Z", target_total_pnl_pct=net)])
    frame = net_frame(raw)
    source = json.loads(frame.iloc[0].outcome_execution_proof)
    assert learning.validate_source(source) == pytest.approx(net)
    assert _costed_close(source["trade"])[1] == pytest.approx(net)
    assert _costed_close(source["trade"])[-1] is True
    checked, report = checked_financial_frame(frame)
    assert checked.iloc[0].target_total_pnl_pct == pytest.approx(net)
    assert supported_financial_training({"financial_training": report}, entry=True)


@pytest.mark.parametrize("declared", [None, "legacy", True, "paper_original_execution_cost_v1"])
def test_financial_artifact_needs_checked_original_closed_cash_basis(declared):
    from ml.financial_targets import checked_financial_frame, supported_financial_training
    from net_financial_fixtures import net_frame
    frame = net_frame(pd.DataFrame([dict(address="synthetic-model-mint", timestamp="2026-09-01T00:00:00Z",
                                        target_total_pnl_pct=5000.)]))
    report = checked_financial_frame(frame)[1]
    assert supported_financial_training({"financial_training": report}, entry=True)
    if declared is None: report.pop("cash_basis_version")
    else: report["cash_basis_version"] = declared
    assert not supported_financial_training({"financial_training": report}, entry=True)


@pytest.mark.asyncio
async def test_fully_legacy_archive_stays_diagnostic_and_is_not_repaired_into_financial_proof(closed, tmp_path):
    from analytics.forward_evidence import _costed_close
    from execution.paper_execution_cost import ENTRY_FIELDS as COST_FIELDS
    from runtime.paper_archive import archive_closed_trade, read_closed_evidence
    source = remove_original_fx(learning.prepare_close(closed.identity, root=closed.root))
    for obj in (source["trade"], source["buy_proof"]["fill"]):
        for name in COST_FIELDS: obj.pop(name)
    trade = source["trade"]
    archive_closed_trade(tmp_path, trade, token=trade["token_address"])
    path = tmp_path / "paper_closed_trades" / (source["trade_id"] + ".json")
    before = path.read_bytes()
    rows, issues = read_closed_evidence(tmp_path)
    assert not issues and len(rows) == 1 and path.read_bytes() == before
    assert _costed_close(rows[0])[-1] is False
    source["payload_sha256"] = learning._hash({k: v for k, v in source.items() if k != "payload_sha256"})
    with pytest.raises(learning.TradeLearningError): learning.validate_source(source)
