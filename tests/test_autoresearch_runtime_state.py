from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from research_loop.evaluator import STATUS_ACCEPTED_REPLAY, STATUS_REJECTED
from research_loop.runtime_state import (
    EVENT_AUTORESEARCH_API_BUDGET_READY,
    EVENT_AUTORESEARCH_CANDIDATES_GENERATED,
    EVENT_AUTORESEARCH_CANDIDATE_ACCEPTED,
    EVENT_AUTORESEARCH_CANDIDATE_FAILED,
    EVENT_AUTORESEARCH_CANDIDATE_REJECTED,
    EVENT_AUTORESEARCH_CYCLE_END,
    EVENT_AUTORESEARCH_CYCLE_START,
    EVENT_AUTORESEARCH_PAPER_PROMOTION_SKIPPED,
    EVENT_AUTORESEARCH_REPLAY_DONE,
    EVENT_AUTORESEARCH_REPLAY_START,
    EVENT_AUTORESEARCH_REPORT_BUNDLE_READY,
    EVENT_AUTORESEARCH_SCOREBOARD_UPDATED,
    EVENT_AUTORESEARCH_START,
    append_event,
    initial_runtime_state,
    read_events,
    read_runtime_state,
    record_cycle_completion,
    record_runtime_error,
    record_runtime_start,
    update_runtime_state,
    write_runtime_state,
)
from research_loop.scheduler import run_autoresearch_cycle


def test_runtime_state_and_events_are_written(tmp_path) -> None:
    config = SimpleNamespace(
        enabled=True,
        mode="paper_replay",
        live_promotion_enabled=False,
        auto_live_promote=False,
    )
    state = initial_runtime_state(config, started_at_utc="2026-06-05T00:00:00+00:00")

    assert state["enabled"] is True
    assert state["mode"] == "paper_replay"
    assert state["live_promotion_enabled"] is False
    assert state["auto_live_promote"] is False

    state_path = write_runtime_state(tmp_path, state)
    update_runtime_state(
        tmp_path,
        {
            "last_status": "completed",
            "cycles_completed": 1,
            "candidates_generated_total": 3,
            "candidates_accepted_total": 1,
            "candidates_rejected_total": 2,
        },
    )
    events_path = append_event(tmp_path, "AUTORESEARCH_CYCLE_END", {"cycle_id": "cycle_1", "status": "completed"})

    persisted = read_runtime_state(tmp_path)
    assert state_path.exists()
    assert persisted["last_status"] == "completed"
    assert persisted["cycles_completed"] == 1
    assert persisted["candidates_generated_total"] == 3
    assert persisted["candidates_accepted_total"] == 1
    assert persisted["candidates_rejected_total"] == 2

    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "AUTORESEARCH_CYCLE_END"
    assert event["cycle_id"] == "cycle_1"
    assert event["status"] == "completed"


def test_runtime_state_survives_multiple_cycles(tmp_path) -> None:
    config = SimpleNamespace(
        enabled=True,
        mode="paper_replay",
        live_promotion_enabled=False,
        auto_live_promote=False,
    )
    record_runtime_start(tmp_path, config, once=False, interval_hours=6.0)

    first_cycle = {
        "status": "completed",
        "failures": [],
        "batches": [
            {
                "candidates_generated": 2,
                "results": [
                    {
                        "proposal_id": "p1",
                        "run_id": "r1",
                        "status": "accepted_replay",
                        "objective_score": 4.2,
                    },
                    {"proposal_id": "p2", "run_id": "r2", "status": "rejected"},
                ],
            }
        ],
    }
    second_cycle = {
        "status": "completed",
        "failures": [],
        "batches": [
            {
                "candidates_generated": 1,
                "results": [{"proposal_id": "p3", "run_id": "r3", "status": "rejected"}],
            }
        ],
    }

    record_cycle_completion(tmp_path, config, first_cycle, next_cycle_at_utc="2026-06-05T06:00:00+00:00")
    state = record_cycle_completion(
        tmp_path,
        config,
        second_cycle,
        next_cycle_at_utc="2026-06-05T12:00:00+00:00",
    )

    assert state["cycles_completed"] == 2
    assert state["candidates_generated_total"] == 3
    assert state["candidates_accepted_total"] == 1
    assert state["candidates_rejected_total"] == 2
    assert state["current_best_policy"]["proposal_id"] == "p1"
    assert state["next_cycle_at_utc"] == "2026-06-05T12:00:00+00:00"
    assert read_events(tmp_path)[0]["event"] == EVENT_AUTORESEARCH_START


def test_runtime_errors_are_recorded(tmp_path) -> None:
    config = SimpleNamespace(
        enabled=True,
        mode="paper_replay",
        live_promotion_enabled=False,
        auto_live_promote=False,
    )
    record_runtime_start(tmp_path, config, once=True, interval_hours=6.0)
    state = record_runtime_error(tmp_path, "boom")
    append_event(tmp_path, "AUTORESEARCH_ERROR", {"error": "boom"})

    assert state["last_status"] == "error"
    assert state["last_error"] == "boom"
    assert read_events(tmp_path)[-1]["event"] == "AUTORESEARCH_ERROR"


@dataclass(frozen=True)
class _FakeBatchResult:
    proposal_id: str
    run_id: str | None
    status: str
    objective_score: float | None = None
    error: str | None = None
    scoreboard_entry: dict | None = None


@dataclass(frozen=True)
class _FakeBatch:
    batch_id: str
    space: str
    candidates_generated: int
    completed: int
    skipped: int
    failed: int
    results: list[_FakeBatchResult]
    scoreboard_path: Path

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "space": self.space,
            "candidates_generated": self.candidates_generated,
            "completed": self.completed,
            "skipped": self.skipped,
            "failed": self.failed,
            "results": [result.__dict__ for result in self.results],
            "scoreboard_path": str(self.scoreboard_path),
        }


def test_scheduler_emits_runtime_cycle_events(tmp_path) -> None:
    def fake_batch_runner(**kwargs):
        batch_id = str(kwargs["batch_id"])
        return _FakeBatch(
            batch_id=batch_id,
            space=str(kwargs["space_name"]),
            candidates_generated=3,
            completed=2,
            skipped=0,
            failed=1,
            scoreboard_path=tmp_path / "data" / "research_runs" / "scoreboard.json",
            results=[
                _FakeBatchResult(
                    proposal_id="p1",
                    run_id="r1",
                    status=STATUS_ACCEPTED_REPLAY,
                    objective_score=7.5,
                    scoreboard_entry={"warnings": [], "rejection_reasons": []},
                ),
                _FakeBatchResult(
                    proposal_id="p2",
                    run_id="r2",
                    status=STATUS_REJECTED,
                    objective_score=-1.0,
                    scoreboard_entry={"warnings": ["w"], "rejection_reasons": ["risk"]},
                ),
                _FakeBatchResult(proposal_id="p3", run_id=None, status="failed", error="replay failed"),
            ],
        )

    result = run_autoresearch_cycle(
        root=tmp_path,
        config={
            "space": "moonshot_micro",
            "max_candidates_per_cycle": 3,
            "auto_paper_promote": False,
            "profitability_demotion_enabled": False,
        },
        seed=5,
        batch_runner_func=fake_batch_runner,
    )

    assert result.status == "completed"
    event_names = [event["event"] for event in read_events(tmp_path)]
    for required in {
        EVENT_AUTORESEARCH_CYCLE_START,
        EVENT_AUTORESEARCH_REPORT_BUNDLE_READY,
        EVENT_AUTORESEARCH_API_BUDGET_READY,
        EVENT_AUTORESEARCH_REPLAY_START,
        EVENT_AUTORESEARCH_CANDIDATES_GENERATED,
        EVENT_AUTORESEARCH_REPLAY_DONE,
        EVENT_AUTORESEARCH_SCOREBOARD_UPDATED,
        EVENT_AUTORESEARCH_CANDIDATE_ACCEPTED,
        EVENT_AUTORESEARCH_CANDIDATE_REJECTED,
        EVENT_AUTORESEARCH_CANDIDATE_FAILED,
        EVENT_AUTORESEARCH_PAPER_PROMOTION_SKIPPED,
        EVENT_AUTORESEARCH_CYCLE_END,
    }:
        assert required in event_names
