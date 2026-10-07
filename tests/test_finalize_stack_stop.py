from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.finalize_stack_stop import RuntimeStateReconciliationError, finalize_runtime_state


def test_finalize_runtime_state_marks_existing_row_stopped(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            create table bot_runtime_state (
                bot_id text primary key,
                updated_at text,
                heartbeat_at text,
                process_state text
            )
            """
        )
        connection.execute(
            "insert into bot_runtime_state values ('main', 'old', 'old', 'running')"
        )
        connection.commit()

    assert finalize_runtime_state(db_path) == 1

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "select process_state, updated_at, heartbeat_at from bot_runtime_state where bot_id='main'"
        ).fetchone()
    assert row is not None
    assert row[0] == "stopped"
    assert row[1] != "old"
    assert row[2] != "old"


def test_finalize_runtime_state_strict_reconciles_existing_bot_row(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "create table bot_runtime_state (bot_id text primary key, updated_at text, heartbeat_at text, process_state text)"
        )
        connection.execute("insert into bot_runtime_state values ('main', 'old', 'old', 'running')")
        connection.commit()

    assert finalize_runtime_state(db_path, require_runtime_state=True) == 1

    with sqlite3.connect(db_path) as connection:
        process_state = connection.execute(
            "select process_state from bot_runtime_state where bot_id='main'"
        ).fetchone()
    assert process_state == ("stopped",)


def test_finalize_runtime_state_resets_optional_background_states(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            create table bot_runtime_state (
                bot_id text primary key,
                updated_at text,
                heartbeat_at text,
                process_state text,
                reports_refresh_state text,
                retrain_state text
            )
            """
        )
        connection.execute(
            "insert into bot_runtime_state values ('main', 'old', 'old', 'running', 'running', 'running')"
        )
        connection.commit()

    assert finalize_runtime_state(db_path, require_runtime_state=True) == 1

    with sqlite3.connect(db_path) as connection:
        state = connection.execute(
            """
            select process_state, reports_refresh_state, retrain_state
              from bot_runtime_state
             where bot_id = 'main'
            """
        ).fetchone()
    assert state == ("stopped", "idle", "idle")


def test_finalize_runtime_state_is_safe_before_database_initialization(tmp_path) -> None:
    assert finalize_runtime_state(tmp_path / "empty.db") == 0


def test_finalize_runtime_state_strict_does_not_create_missing_database(tmp_path) -> None:
    db_path = tmp_path / "missing.db"

    with pytest.raises(RuntimeStateReconciliationError, match="runtime database does not exist"):
        finalize_runtime_state(db_path, require_runtime_state=True)

    assert not db_path.exists()


def test_finalize_runtime_state_strict_requires_table_and_bot_row(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("create table placeholder (value text)")
        connection.commit()

    with pytest.raises(RuntimeStateReconciliationError, match="bot_runtime_state table not found"):
        finalize_runtime_state(db_path, require_runtime_state=True)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "create table bot_runtime_state (bot_id text primary key, updated_at text, heartbeat_at text, process_state text)"
        )
        connection.commit()

    with pytest.raises(RuntimeStateReconciliationError, match="bot_runtime_state row not found"):
        finalize_runtime_state(db_path, require_runtime_state=True)


def test_finalize_script_runs_by_path_like_stop_stack(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "create table bot_runtime_state (bot_id text primary key, updated_at text, heartbeat_at text, process_state text)"
        )
        connection.execute("insert into bot_runtime_state values ('main', 'old', 'old', 'running')")
        connection.commit()

    result = subprocess.run(
        [
            sys.executable,
            str(Path("scripts/finalize_stack_stop.py").resolve()),
            "--db-path",
            str(db_path),
            "--requested-by",
            "pytest",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert '"process_state": "stopped"' in result.stdout


def test_finalize_script_strict_mode_fails_closed_with_machine_readable_error(tmp_path) -> None:
    db_path = tmp_path / "missing.db"
    result = subprocess.run(
        [
            sys.executable,
            str(Path("scripts/finalize_stack_stop.py").resolve()),
            "--db-path",
            str(db_path),
            "--requested-by",
            "start_stack_stale_lock",
            "--require-runtime-state",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert not db_path.exists()
    assert '"status": "error"' in result.stderr
    assert '"runtime_state_required": true' in result.stderr
    assert "runtime database does not exist" in result.stderr
