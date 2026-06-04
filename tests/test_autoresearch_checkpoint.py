from __future__ import annotations

from research_loop.checkpoint import (
    candidate_duplicate_check,
    changes_hash,
    empty_checkpoint,
    load_checkpoint,
    record_checkpoint_run,
    save_checkpoint,
)


def _candidate(proposal_id: str, value: str = "75") -> dict:
    return {
        "proposal_id": proposal_id,
        "changes": {"MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL": value},
    }


def test_changes_hash_is_stable_for_same_changes() -> None:
    assert changes_hash(_candidate("a")) == changes_hash(_candidate("b"))


def test_checkpoint_detects_duplicate_by_proposal_id_changes_and_config_hash() -> None:
    checkpoint = empty_checkpoint()
    candidate = _candidate("ar_checkpoint_001")
    record_checkpoint_run(
        checkpoint,
        run_id="run1",
        candidate_policy=candidate,
        status="accepted_replay",
        config_hash="cfg1",
        objective_score=10.0,
    )

    by_proposal = candidate_duplicate_check(_candidate("ar_checkpoint_001", "80"), checkpoint)
    by_changes = candidate_duplicate_check(_candidate("ar_checkpoint_002"), checkpoint)
    by_config = candidate_duplicate_check(_candidate("ar_checkpoint_003", "90"), checkpoint, config_hash="cfg1")

    assert by_proposal.duplicate
    assert "proposal_id" in by_proposal.reasons
    assert by_changes.duplicate
    assert "changes_hash" in by_changes.reasons
    assert by_config.duplicate
    assert "config_hash" in by_config.reasons


def test_checkpoint_save_and_load_roundtrip(tmp_path) -> None:
    checkpoint = empty_checkpoint()
    record_checkpoint_run(
        checkpoint,
        run_id="run1",
        candidate_policy=_candidate("ar_checkpoint_001"),
        status="rejected",
    )
    save_checkpoint(checkpoint, root=tmp_path)

    loaded = load_checkpoint(tmp_path)

    assert loaded["proposal_ids"] == ["ar_checkpoint_001"]
    assert loaded["runs"][0]["status"] == "rejected"
