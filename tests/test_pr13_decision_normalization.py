from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from analytics.report_utils import dedupe_candidate_rows, load_candidate_outcomes
from features.decision_store import append_decision, normalize_decision_action
from ml.data_contract import normalize_candidate_event_row


def test_candidate_event_contract_has_id_blockers_snapshot_and_lineage() -> None:
    row = {
        "event_type": "candidate_decision",
        "address": "MINT1",
        "ts_utc": "2026-07-07T10:00:00+00:00",
        "decision_action": "rejected",
        "reason": "no_liq",
        "stage": "basic_filter",
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_green_candle_sniper",
        "run_id": "run-13",
        "discovered_via": "pumpfun",
        "score_total": 42,
    }

    normalized = normalize_candidate_event_row(row, source_file="candidate_outcomes.jsonl", row_index=3)
    normalized_again = normalize_candidate_event_row(row, source_file="candidate_outcomes.jsonl", row_index=3)

    assert normalized["decision_id"] == normalized_again["decision_id"]
    assert normalized["candidate_stage"] == "basic_filter"
    assert normalized["decision"] == "reject"
    assert normalized["outcome"] == "reject"
    assert normalized["blockers"] == ["liquidity_missing"]
    assert normalized["blocker"] == "liquidity_missing"
    assert normalized["run_id"] == "run-13"
    assert normalized["source"] == "candidate_decision"
    assert normalized["lane"] == "pump_early_green_candle_sniper"
    assert normalized["feature_snapshot"]["score_total"] == 42
    assert "reason" not in normalized["feature_snapshot"]
    assert normalized["row_lineage"] == {"source_file": "candidate_outcomes.jsonl", "row_index": 3}


def test_blocker_taxonomy_normalizes_provider_and_route_reasons() -> None:
    provider = normalize_candidate_event_row(
        {
            "event_type": "candidate_decision",
            "address": "MINT2",
            "ts_utc": "2026-07-07T10:01:00+00:00",
            "decision_action": "wait",
            "reason": "provider_degraded:jupiter",
        }
    )
    route = normalize_candidate_event_row(
        {
            "event_type": "candidate_decision",
            "address": "MINT3",
            "ts_utc": "2026-07-07T10:02:00+00:00",
            "decision_action": "wait",
            "reason": "no_route",
        }
    )

    assert provider["blockers"] == ["provider_degraded"]
    assert route["blockers"] == ["route_missing"]
    assert route["decision"] == "execution_blocked"


def test_strategy_live_intent_without_order_is_observe_and_open() -> None:
    row = {
        "event_type": "strategy_decision",
        "address": "MINT-LIVE-INTENT",
        "ts_utc": "2026-07-15T00:25:15+00:00",
        "action": "live",
        # Re-normalization must repair rows persisted by the old contract.
        "decision": "buy",
        "outcome": "bought",
        "reason": "paper_aggressive_scorecard_negative",
    }

    normalized = normalize_candidate_event_row(row)

    assert normalized["decision"] == "observe"
    assert normalized["outcome"] == "open"
    assert normalized["raw_action"] == "live"
    assert normalized["decision_intent"] == "live"
    assert "action" not in normalized["feature_snapshot"]


def test_strategy_live_with_order_evidence_is_buy_and_bought() -> None:
    normalized = normalize_candidate_event_row(
        {
            "event_type": "strategy_decision",
            "address": "MINT-LIVE-ORDERED",
            "ts_utc": "2026-07-15T00:26:15+00:00",
            "action": "live",
            "order_id": "order-123",
        }
    )

    assert normalized["decision"] == "buy"
    assert normalized["outcome"] == "bought"
    assert normalized["raw_action"] == "live"


def test_decision_store_persists_live_strategy_intent_without_fake_buy(tmp_path: Path) -> None:
    source = {
        "event_type": "strategy_decision",
        "address": "MINT-LEDGER-LIVE",
        "ts_utc": "2026-07-15T00:27:15+00:00",
        "action": "live",
        "reason": "strategy_ready",
    }

    assert normalize_decision_action("live", source) == "observe"
    persisted = append_decision(source, path=tmp_path / "decision_ledger.jsonl")

    assert persisted["decision"] == "observe"
    assert persisted["outcome"] == "open"
    assert persisted["raw_action"] == "live"
    assert persisted["decision_intent"] == "live"


def test_report_utils_adds_row_lineage_and_can_dedupe_by_decision_id(tmp_path: Path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        {
            "decision_id": "same-decision",
            "event_type": "candidate_decision",
            "address": "MINT4",
            "ts_utc": "2026-07-07T10:00:00+00:00",
            "decision_action": "rejected",
            "reason": "basic_filter",
        },
        {
            "decision_id": "same-decision",
            "event_type": "candidate_decision",
            "address": "MINT4",
            "ts_utc": "2026-07-07T10:00:00+00:00",
            "decision_action": "rejected",
            "reason": "basic_filter",
        },
    ]
    (metrics / "candidate_outcomes.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    loaded = load_candidate_outcomes(tmp_path)
    deduped = load_candidate_outcomes(tmp_path, dedupe=True)

    assert len(loaded) == 2
    assert loaded[0]["row_lineage"]["source_file"].endswith("candidate_outcomes.jsonl")
    assert loaded[0]["row_lineage"]["row_index"] == 0
    assert len(deduped) == 1
    assert dedupe_candidate_rows(loaded)[0]["decision_id"] == "same-decision"


def test_research_runtime_writes_normalized_candidate_decision(monkeypatch, tmp_path: Path) -> None:
    import analytics.research_runtime as research_runtime
    events_path = tmp_path / "candidate_outcomes.jsonl"
    captured: list[dict] = []

    monkeypatch.setattr(research_runtime, "RESEARCH_EVENTS_PATH", events_path)
    monkeypatch.setattr(
        research_runtime,
        "CFG",
        SimpleNamespace(RESEARCH_LANE_ENABLED=True, RESEARCH_DECISION_DEDUP_TTL_S=0),
    )
    monkeypatch.setattr(research_runtime, "append_decision", lambda payload: captured.append(payload))
    research_runtime._SEEN.clear()

    research_runtime.record_candidate_decision(
        {
            "address": "MINT5",
            "entry_regime": "pump_early",
            "entry_lane": "pump_early_green_candle_sniper",
            "run_id": "run-pr13",
            "score_total": 66,
        },
        action="wait",
        reason="no_route",
        stage="execution_guard",
    )

    written = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
    assert written["decision_id"]
    assert written["decision"] == "execution_blocked"
    assert written["blockers"] == ["route_missing"]
    assert written["feature_snapshot"]["score_total"] == 66
    assert captured[0]["decision_id"] == written["decision_id"]
    assert captured[0]["features_snapshot"] == written["feature_snapshot"]
