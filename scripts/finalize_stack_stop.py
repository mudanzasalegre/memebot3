from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.database import DB_PATH


class RuntimeStateReconciliationError(RuntimeError):
    """Raised when a required persisted runtime state cannot be reconciled."""


def finalize_runtime_state(
    db_path: str | Path,
    *,
    bot_id: str = "main",
    require_runtime_state: bool = False,
) -> int:
    """Mark the persisted runtime stopped after the process tree is gone."""

    resolved = Path(db_path).expanduser().resolve()
    if require_runtime_state and not resolved.is_file():
        raise RuntimeStateReconciliationError(f"runtime database does not exist: {resolved}")

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    with sqlite3.connect(resolved, timeout=15.0) as connection:
        table_exists = connection.execute(
            "select 1 from sqlite_master where type='table' and name='bot_runtime_state'"
        ).fetchone()
        if table_exists is None:
            if require_runtime_state:
                raise RuntimeStateReconciliationError(
                    f"bot_runtime_state table not found in runtime database: {resolved}"
                )
            return 0

        columns = {
            str(row[1])
            for row in connection.execute("pragma table_info(bot_runtime_state)").fetchall()
        }
        assignments = [
            "process_state = 'stopped'",
            "updated_at = ?",
            "heartbeat_at = ?",
        ]
        if "reports_refresh_state" in columns:
            assignments.append("reports_refresh_state = 'idle'")
        if "retrain_state" in columns:
            assignments.append("retrain_state = 'idle'")
        cursor = connection.execute(
            f"update bot_runtime_state set {', '.join(assignments)} where bot_id = ?",
            (now, now, str(bot_id)),
        )
        rows_updated = int(cursor.rowcount or 0)
        if require_runtime_state and rows_updated != 1:
            raise RuntimeStateReconciliationError(
                f"bot_runtime_state row not found for bot_id={bot_id!r} in {resolved}"
            )
        connection.commit()
        return rows_updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Finalize persisted bot state after stop_stack verified zero processes.")
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--bot-id", default="main")
    parser.add_argument("--requested-by", default="stop_stack")
    parser.add_argument(
        "--require-runtime-state",
        action="store_true",
        help="Fail instead of creating/accepting a missing database, table, or bot row.",
    )
    args = parser.parse_args()

    try:
        updated = finalize_runtime_state(
            args.db_path,
            bot_id=args.bot_id,
            require_runtime_state=args.require_runtime_state,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "bot_id": args.bot_id,
                    "db_path": str(Path(args.db_path).expanduser().resolve()),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "requested_by": args.requested_by,
                    "runtime_state_required": bool(args.require_runtime_state),
                    "status": "error",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "bot_id": args.bot_id,
                "db_path": str(Path(args.db_path).resolve()),
                "process_state": "stopped",
                "requested_by": args.requested_by,
                "rows_updated": updated,
                "runtime_state_required": bool(args.require_runtime_state),
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
