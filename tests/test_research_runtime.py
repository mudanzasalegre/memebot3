from __future__ import annotations

import datetime as dt
import importlib.util
import json
import math
import sys
import types
from pathlib import Path
from types import SimpleNamespace


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_ORIG_ANALYTICS = sys.modules.get("analytics")
_ORIG_AUDIT = sys.modules.get("analytics.audit")
_ORIG_NUMPY = sys.modules.get("numpy")
_ORIG_PANDAS = sys.modules.get("pandas")


class _Timestamp:
    tzinfo = dt.timezone.utc

    def tz_localize(self, _tz: str) -> "_Timestamp":
        return self

    def isoformat(self) -> str:
        return dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).isoformat()


_analytics_stub = types.ModuleType("analytics")
_analytics_stub.__path__ = []  # type: ignore[attr-defined]
_audit_stub = types.ModuleType("analytics.audit")
_audit_stub.normalize_candidate_outcomes_frame = lambda frame: frame
_audit_stub.write_normalized_candidate_outcomes = lambda *_args, **_kwargs: None
_analytics_stub.audit = _audit_stub
_numpy_stub = types.ModuleType("numpy")
_numpy_stub.floating = float
_numpy_stub.integer = int
_numpy_stub.isfinite = math.isfinite
_pandas_stub = types.ModuleType("pandas")
_pandas_stub.Timestamp = _Timestamp

sys.modules["analytics"] = _analytics_stub
sys.modules["analytics.audit"] = _audit_stub
sys.modules["numpy"] = _numpy_stub
sys.modules["pandas"] = _pandas_stub

_MODULE_PATH = _ROOT / "analytics" / "research_runtime.py"
_SPEC = importlib.util.spec_from_file_location("research_runtime_under_test", _MODULE_PATH)
assert _SPEC and _SPEC.loader
research_runtime = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = research_runtime
_SPEC.loader.exec_module(research_runtime)

if _ORIG_ANALYTICS is not None:
    sys.modules["analytics"] = _ORIG_ANALYTICS
else:
    sys.modules.pop("analytics", None)
if _ORIG_AUDIT is not None:
    sys.modules["analytics.audit"] = _ORIG_AUDIT
else:
    sys.modules.pop("analytics.audit", None)
if _ORIG_NUMPY is not None:
    sys.modules["numpy"] = _ORIG_NUMPY
else:
    sys.modules.pop("numpy", None)
if _ORIG_PANDAS is not None:
    sys.modules["pandas"] = _ORIG_PANDAS
else:
    sys.modules.pop("pandas", None)


def test_live_rank_gate_uses_supported_alternative_when_picked_is_sparse(monkeypatch, tmp_path: Path) -> None:
    thresholds_path = tmp_path / "research_thresholds.json"
    generated_at = dt.datetime(2026, 4, 16, 4, 43, tzinfo=dt.timezone.utc)
    thresholds_path.write_text(
        json.dumps(
            {
                "generated_at_utc": generated_at.isoformat(),
                "regimes": {
                    "pump_early": {
                        "rank_score": {
                            "activation_ready": 1,
                            "picked_rank_score": 42.08,
                            "selected_rows_at_picked": 6,
                            "avg_realized_pnl_pct_at_picked": 95.68,
                            "alternatives": {
                                "max_expected_pnl": {
                                    "threshold": 0.4208,
                                    "selected_rows": 6,
                                    "avg_realized_pnl_pct": 95.68,
                                },
                                "youden": {
                                    "threshold": 0.1375,
                                    "selected_rows": 130,
                                    "avg_realized_pnl_pct": 8.04,
                                },
                                "max_f1": {
                                    "threshold": 0.01,
                                    "selected_rows": 211,
                                    "avg_realized_pnl_pct": 4.29,
                                },
                            },
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(research_runtime, "RESEARCH_THRESHOLDS_JSON", thresholds_path)
    monkeypatch.setattr(
        research_runtime,
        "CFG",
        SimpleNamespace(
            LIVE_RANK_SCORE_FALLBACK_MIN=12.5,
            LIVE_RANK_SCORE_MIN_SELECTED_ROWS=20,
            LIVE_RANK_SCORE_MIN_AVG_PNL_PCT=3.0,
            STRATEGY_SCORECARD_MAX_AGE_MIN=240.0,
        ),
    )

    gate = research_runtime.load_live_rank_gate("pump_early", now=generated_at + dt.timedelta(minutes=1))

    assert gate["source"] == "research_thresholds_alternative:youden"
    assert gate["activation_ready"] is True
    assert gate["threshold"] == 13.750000000000002
    assert gate["selected_rows_at_picked"] == 130
    assert gate["picked_selected_rows_at_picked"] == 6


def test_record_shadow_open_ignores_duplicate_context_keys(monkeypatch, tmp_path: Path) -> None:
    events_path = tmp_path / "candidate_outcomes.jsonl"
    portfolio_path = tmp_path / "research_portfolio.json"
    monkeypatch.setattr(research_runtime, "RESEARCH_EVENTS_PATH", events_path)
    monkeypatch.setattr(research_runtime, "RESEARCH_PORTFOLIO_PATH", portfolio_path)
    research_runtime._OPEN_SHADOWS.clear()

    research_runtime.record_shadow_open(
        "addr-1",
        payload={
            "decision_action": "shadow",
            "reason": "live_profit_gate:rank",
            "stage": "decision",
            "shadow_kind": "execution",
            "regime": "pump_early",
        },
        shadow_kind="research",
    )

    row = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["decision_action"] == "research_shadow_open"
    assert row["shadow_kind"] == "research"


def test_record_candidate_stage_preserves_total_rank_score(monkeypatch, tmp_path: Path) -> None:
    events_path = tmp_path / "candidate_outcomes.jsonl"
    monkeypatch.setattr(research_runtime, "RESEARCH_EVENTS_PATH", events_path)
    research_runtime._SEEN.clear()

    research_runtime.record_candidate_stage(
        {
            "address": "addr-rank",
            "entry_regime": "pump_early",
            "discovered_via": "dex",
            "score_total": 45,
            "age_minutes": 8.0,
            "liquidity_usd": 12_000.0,
            "market_cap_usd": 30_000.0,
            "snapshot_missing_fields": 4,
            "coverage_core_fields": 3,
            "has_jupiter_route": 1,
        },
        stage="late_funnel",
        proba=0.0,
        threshold=0.0,
        rank_info={
            "rank_score": 52.7,
            "components": {
                "score": 11.25,
                "liq": 6.0,
            },
        },
    )

    row = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["rank_score"] == 52.7
    assert row["rank_score_component"] == 11.25
    assert row["rank_liq"] == 6.0
