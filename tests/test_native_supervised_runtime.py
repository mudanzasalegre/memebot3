"""Actual cold native orchestration; no operator runtime or live providers."""
from test_native_paper_lifecycle import native_case


def test_native_cold_supervisor_discovers_exact_paper_trade_and_drains_actual_report_child(tmp_path):
    row = native_case(tmp_path, case='rpc_birth', momentum=1000000., monitor=True,
        supervised_runtime=True, supervised_hold_report=True)
    assert row['positions'][0]['amount_sol'] == .1 and row['positions'][0]['closed'] is True
    assert row['export_rows'] == 1 and row['frozen_signal'] == 1000000.
    assert row['progression'][-2]['qty'] > 0 and row['progression'][-2]['peak'] > 900000
    assert row['pending_buys'] == row['pending_sells'] == row['pending_dispatch'] == 0
    assert row['completion_confirmed'] and row['persisted_runtime_state'] == 'stopped'


def test_native_cold_supervisor_keeps_route_veto_and_drains_actual_report_child(tmp_path):
    row = native_case(tmp_path, case='rpc_birth', supervised_runtime=True,
        supervised_veto='missing_route', supervised_hold_report=True)
    assert row['positions'] == [] and row['stats']['actual_paper_buys'] == 0
    assert row['pending_buys'] == row['pending_sells'] == row['pending_dispatch'] == 0
    assert row['completion_confirmed'] and row['persisted_runtime_state'] == 'stopped'
