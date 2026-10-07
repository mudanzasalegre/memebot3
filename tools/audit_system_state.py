"""Read-only, reproducible inventory of evidence. Never includes environment secrets."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sqlite3
from pathlib import Path


def audit(root: Path) -> dict:
    # Snapshot before SQLite opens its own sidecars; these are not run evidence.
    files = [p for folder in ("logs", "data", "ml/models") for p in (root / folder).rglob("*")
             if p.is_file() and not p.name.endswith(("-wal", "-shm"))]
    newest = max(files, key=lambda p: p.stat().st_mtime) if files else None
    newest_time = newest.stat().st_mtime if newest else None
    runs: dict[str, dict] = {}
    sources = {}
    for name in ("runtime_events", "decision_ledger", "candidate_outcomes"):
        path = root / "data" / "metrics" / f"{name}.jsonl"
        if not path.exists():
            sources[name] = {"missing": True}
            continue
        invalid = count = 0
        for line in path.open(encoding="utf-8"):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("non-object")
            except (ValueError, TypeError):
                invalid += 1
                continue
            count += 1
            run = runs.setdefault(str(row.get("run_id") or "legacy"), {
                "rows": collections.Counter(), "events": collections.Counter(),
                "stages": collections.Counter(), "bootstrap_failures": collections.Counter(),
                "first_event_at": None, "last_event_at": None,
            })
            run["rows"][name] += 1
            if name == "runtime_events":
                event = str(row.get("event_type") or row.get("event") or "unknown")
                run["events"][event] += 1
                timestamp = row.get("ts_utc") or row.get("timestamp")
                if timestamp:
                    run["first_event_at"] = min(run["first_event_at"] or timestamp, timestamp)
                    run["last_event_at"] = max(run["last_event_at"] or timestamp, timestamp)
                if event == "paper_bootstrap_eval":
                    for failure in str(row.get("hard_failures") or "").split(","):
                        if failure:
                            run["bootstrap_failures"][failure] += 1
            elif name == "decision_ledger":
                run["stages"][str(row.get("stage") or row.get("decision_stage") or "unknown")] += 1
        sources[name] = {"rows": count, "invalid_lines": invalid, "bytes": path.stat().st_size}
    database = root / "data" / "memebotdatabase.db"
    db = {}
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            for name, in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                if name in {"tokens", "positions", "trades", "bot_runtime_state"}:
                    db[name] = {"rows": conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]}
                if name == "bot_runtime_state":
                    allowed = {"run_id", "process_state", "report_state", "retrain_state", "last_error", "monitor_last_ok_at", "stopped_at"}
                    db[name]["state"] = [{k: row[k] for k in row.keys() if k in allowed}
                                           for row in conn.execute('SELECT * FROM bot_runtime_state')]
    latest_model = None
    model_dirs = sorted((root / "ml" / "models").glob("*/model.meta.json"))
    if model_dirs:
        path = model_dirs[-1]
        metadata = json.loads(path.read_text(encoding="utf-8"))
        latest_model = {"model_id": path.parent.name, "metadata_keys": sorted(metadata),
                        "activation": metadata.get("activation"), "activation_ready": metadata.get("activation_ready"),
                        "threshold_result": metadata.get("threshold_result"),
                        "dataset": metadata.get("strict_productive_dataset")}
    return {
        "schema_version": 1, "inspected_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "evidence_latest_modified_at_utc": dt.datetime.fromtimestamp(newest_time, dt.timezone.utc).isoformat() if newest_time else None,
        "evidence_latest_event_at_utc": max((r["last_event_at"] for key, r in runs.items()
                                              if key not in {"legacy", "SMOKE"} and r["last_event_at"]), default=None),
        "evidence_latest_path": str(newest.relative_to(root)) if newest else None,
        "runtime_lock_exists": any((root / name).exists() for name in ("run_bot.lock", "data/run_bot.lock")),
        "sources": sources, "database": db, "runs": runs, "latest_model": latest_model,
        "active_model_exists": (root / "ml" / "model.pkl").exists(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(audit(args.root.resolve()), indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding="utf-8")
    print(result)


if __name__ == "__main__":
    main()
