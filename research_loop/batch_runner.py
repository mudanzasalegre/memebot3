from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from research_loop.bandit import suggest_spaces
from research_loop.candidate_generator import generate_candidate_policies, write_candidate_policies
from research_loop.checkpoint import (
    candidate_duplicate_check,
    checkpoint_path,
    load_checkpoint,
    record_checkpoint_run,
    save_checkpoint,
)
from research_loop.evaluator import EvaluationResult, evaluate_replay_candidate
from research_loop.paths import metrics_dir, project_root, research_runs_dir
from research_loop.replay_runner import REPLAY_REPORTS, ReplayRunResult, extract_replay_metrics, run_research_replay_from_sandbox
from research_loop.sandbox import SandboxResult, create_candidate_sandbox
from research_loop.scoreboard import load_scoreboard, record_evaluation
from research_loop.api_budget import build_api_budget_report


@dataclass(frozen=True)
class BatchCandidateResult:
    proposal_id: str
    run_id: str | None
    status: str
    skipped: bool = False
    duplicate_reasons: list[str] = field(default_factory=list)
    error: str | None = None
    objective_score: float | None = None
    scoreboard_entry: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "run_id": self.run_id,
            "status": self.status,
            "skipped": self.skipped,
            "duplicate_reasons": list(self.duplicate_reasons),
            "error": self.error,
            "objective_score": self.objective_score,
            "scoreboard_entry": self.scoreboard_entry,
        }


@dataclass(frozen=True)
class BatchRunResult:
    batch_id: str
    space: str
    candidates_generated: int
    completed: int
    skipped: int
    failed: int
    results: list[BatchCandidateResult]
    batch_dir: Path
    checkpoint_path: Path
    scoreboard_path: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "space": self.space,
            "candidates_generated": self.candidates_generated,
            "completed": self.completed,
            "skipped": self.skipped,
            "failed": self.failed,
            "results": [result.as_dict() for result in self.results],
            "batch_dir": str(self.batch_dir),
            "checkpoint_path": str(self.checkpoint_path),
            "scoreboard_path": str(self.scoreboard_path),
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-").lower()
    return cleaned or "batch"


def _default_batch_id(space: str, seed: int | None) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    seed_part = "noseed" if seed is None else f"s{seed}"
    return _safe_id(f"batch_{space}_{seed_part}_{stamp}")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _copy_baseline_snapshot(root: Path, snapshot_dir: Path) -> list[str]:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for name in REPLAY_REPORTS:
        source = metrics_dir(root) / name
        if not source.exists():
            failures.append(f"missing_report:{name}")
            continue
        shutil.copy2(source, snapshot_dir / name)
    return failures


def build_batch_baseline(
    *,
    root: str | Path | None = None,
    batch_dir: str | Path,
    regenerate: bool = False,
    regenerate_func: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved_root = project_root(root)
    resolved_batch_dir = Path(batch_dir)
    warnings: list[str] = []
    failures: list[str] = []
    if regenerate:
        try:
            if regenerate_func is None:
                from analytics.core_report_scheduler import regenerate_core_reports

                summary = regenerate_core_reports(resolved_root, include_test_events=False)
            else:
                summary = regenerate_func(resolved_root)
            regen_warnings = summary.get("warnings") if isinstance(summary, dict) else {}
            if isinstance(regen_warnings, dict):
                warnings.extend(f"report_warning:{name}:{message}" for name, message in regen_warnings.items())
        except Exception as exc:
            failures.append(f"baseline_regenerate_failed:{exc}")

    snapshot_dir = resolved_batch_dir / "baseline_snapshot"
    failures.extend(_copy_baseline_snapshot(resolved_root, snapshot_dir))
    api_budget = build_api_budget_report(resolved_root, write=True)
    metrics = extract_replay_metrics(snapshot_dir, api_budget=api_budget)
    if failures:
        metrics["failed"] = True
    payload = {
        "generated_at_utc": utc_now(),
        "metrics": metrics,
        "warnings": warnings,
        "failures": failures,
        "snapshot_dir": str(snapshot_dir),
    }
    _write_json(resolved_batch_dir / "baseline_metrics.json", metrics)
    _write_json(resolved_batch_dir / "baseline_report.json", payload)
    return payload


def _choose_space(space_name: str, root: Path, seed: int | None) -> str:
    if space_name not in {"auto", "bandit"}:
        return space_name
    suggestion = suggest_spaces(load_scoreboard(root), n=1, seed=seed)
    return suggestion.spaces[0] if suggestion.spaces else "moonshot_micro"


def _objective_score(result: EvaluationResult) -> float | None:
    return result.objective.score if result.objective is not None else None


def run_research_batch(
    *,
    space_name: str,
    n: int,
    seed: int | None = None,
    mode: str = "seeded_random",
    root: str | Path | None = None,
    batch_id: str | None = None,
    regenerate_baseline: bool = False,
    regenerate_replay: bool = True,
    regenerate_func: Callable[[Path], dict[str, Any]] | None = None,
    min_closed_trades: int = 0,
    candidates: list[dict[str, Any]] | None = None,
    replay_func: Callable[[SandboxResult], ReplayRunResult] | None = None,
    baseline_metrics: dict[str, Any] | None = None,
    write_candidates: bool = True,
) -> BatchRunResult:
    resolved_root = project_root(root)
    resolved_space = _choose_space(space_name, resolved_root, seed)
    resolved_batch_id = _safe_id(batch_id or _default_batch_id(resolved_space, seed))
    batch_dir = research_runs_dir(resolved_root) / "batches" / resolved_batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    if candidates is None:
        candidates = generate_candidate_policies(space_name=resolved_space, n=n, mode=mode, seed=seed)
    if write_candidates:
        write_candidate_policies(candidates, root=resolved_root)

    checkpoint = load_checkpoint(resolved_root)
    if baseline_metrics is None:
        baseline_report = build_batch_baseline(
            root=resolved_root,
            batch_dir=batch_dir,
            regenerate=regenerate_baseline,
            regenerate_func=regenerate_func,
        )
        baseline_payload = dict(baseline_report.get("metrics") or {})
    else:
        baseline_payload = dict(baseline_metrics)
        _write_json(batch_dir / "baseline_metrics.json", baseline_payload)

    results: list[BatchCandidateResult] = []
    for candidate in candidates:
        proposal_id = str(candidate.get("proposal_id") or "")
        duplicate = candidate_duplicate_check(candidate, checkpoint)
        if duplicate.duplicate:
            record_checkpoint_run(
                checkpoint,
                run_id=f"skipped_{proposal_id}",
                candidate_policy=candidate,
                status="skipped_duplicate",
                error=",".join(duplicate.reasons),
            )
            save_checkpoint(checkpoint, root=resolved_root)
            results.append(
                BatchCandidateResult(
                    proposal_id=proposal_id,
                    run_id=None,
                    status="skipped_duplicate",
                    skipped=True,
                    duplicate_reasons=duplicate.reasons,
                )
            )
            continue

        sandbox: SandboxResult | None = None
        run_id = proposal_id or None
        try:
            sandbox = create_candidate_sandbox(candidate, root=resolved_root, run_id=proposal_id)
            duplicate_after_sandbox = candidate_duplicate_check(candidate, checkpoint, config_hash=sandbox.config_hash)
            if duplicate_after_sandbox.duplicate:
                record_checkpoint_run(
                    checkpoint,
                    run_id=sandbox.run_id,
                    candidate_policy=candidate,
                    status="skipped_duplicate",
                    config_hash=sandbox.config_hash,
                    error=",".join(duplicate_after_sandbox.reasons),
                )
                save_checkpoint(checkpoint, root=resolved_root)
                results.append(
                    BatchCandidateResult(
                        proposal_id=proposal_id,
                        run_id=sandbox.run_id,
                        status="skipped_duplicate",
                        skipped=True,
                        duplicate_reasons=duplicate_after_sandbox.reasons,
                    )
                )
                continue

            if replay_func is None:
                replay = run_research_replay_from_sandbox(
                    sandbox,
                    root=resolved_root,
                    regenerate=regenerate_replay,
                    regenerate_func=regenerate_func,
                )
            else:
                replay = replay_func(sandbox)

            _write_json(sandbox.run_dir / "baseline_metrics.json", baseline_payload)
            evaluation = evaluate_replay_candidate(
                candidate,
                baseline_payload,
                replay.replay_metrics,
                min_closed_trades=min_closed_trades,
            )
            scoreboard_entry = record_evaluation(
                run_id=sandbox.run_id,
                candidate_policy=candidate,
                evaluation_result=evaluation,
                root=resolved_root,
            )
            record_checkpoint_run(
                checkpoint,
                run_id=sandbox.run_id,
                candidate_policy=candidate,
                status=evaluation.status,
                config_hash=sandbox.config_hash,
                objective_score=_objective_score(evaluation),
            )
            save_checkpoint(checkpoint, root=resolved_root)
            results.append(
                BatchCandidateResult(
                    proposal_id=proposal_id,
                    run_id=sandbox.run_id,
                    status=evaluation.status,
                    objective_score=_objective_score(evaluation),
                    scoreboard_entry=scoreboard_entry,
                )
            )
        except Exception as exc:
            resolved_run_id = sandbox.run_id if sandbox is not None else run_id
            record_checkpoint_run(
                checkpoint,
                run_id=resolved_run_id or f"failed_{proposal_id}",
                candidate_policy=candidate,
                status="failed",
                config_hash=sandbox.config_hash if sandbox is not None else None,
                error=str(exc),
            )
            save_checkpoint(checkpoint, root=resolved_root)
            results.append(
                BatchCandidateResult(
                    proposal_id=proposal_id,
                    run_id=resolved_run_id,
                    status="failed",
                    error=str(exc),
                )
            )
            continue

    completed = len([result for result in results if not result.skipped and result.status != "failed"])
    skipped = len([result for result in results if result.skipped])
    failed = len([result for result in results if result.status == "failed"])
    payload = {
        "generated_at_utc": utc_now(),
        "batch_id": resolved_batch_id,
        "space": resolved_space,
        "candidates_generated": len(candidates),
        "completed": completed,
        "skipped": skipped,
        "failed": failed,
        "results": [result.as_dict() for result in results],
    }
    _write_json(batch_dir / "batch_result.json", payload)
    return BatchRunResult(
        batch_id=resolved_batch_id,
        space=resolved_space,
        candidates_generated=len(candidates),
        completed=completed,
        skipped=skipped,
        failed=failed,
        results=results,
        batch_dir=batch_dir,
        checkpoint_path=checkpoint_path(resolved_root),
        scoreboard_path=research_runs_dir(resolved_root) / "scoreboard.json",
    )


__all__ = [
    "BatchCandidateResult",
    "BatchRunResult",
    "build_batch_baseline",
    "run_research_batch",
]
