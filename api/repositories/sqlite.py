from __future__ import annotations

import sqlite3
from pathlib import Path


def table_row_counts(db_path: Path, tables: tuple[str, ...]) -> dict[str, int | None]:
    if not db_path.exists():
        return {table: None for table in tables}

    counts: dict[str, int | None] = {table: None for table in tables}
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing = {str(row[0]) for row in cursor.fetchall()}
        for table in tables:
            if table not in existing:
                counts[table] = None
                continue
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            row = cursor.fetchone()
            counts[table] = int(row[0]) if row else 0
    return counts
