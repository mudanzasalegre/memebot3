from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any


UTC = dt.timezone.utc


def parse_timestamp(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(value), tz=UTC)
        except Exception:
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(normalized)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def read_json_file(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    rows.append(parsed)
    except Exception:
        return []
    return rows


def load_jsonl_tail_rows(path: Path, *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_line in tail_text_lines(path, limit=limit):
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except Exception:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def file_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return int(path.stat().st_size)
    except Exception:
        return 0


def count_text_lines(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    count = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                count += chunk.count(b"\n")
    except Exception:
        return 0
    return count


def file_mtime(path: Path) -> dt.datetime | None:
    if not path.exists():
        return None
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except Exception:
        return None


def newest_matching_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    matches = sorted(directory.glob(pattern), key=lambda item: item.stat().st_mtime)
    return matches[-1] if matches else None


def tail_text_lines(path: Path, *, limit: int) -> list[str]:
    if limit <= 0 or not path.exists() or not path.is_file():
        return []

    try:
        size = path.stat().st_size
    except Exception:
        return []
    if size <= 0:
        return []

    chunk_size = 8192
    data = bytearray()
    line_breaks = 0
    try:
        with path.open("rb") as handle:
            offset = size
            while offset > 0 and line_breaks <= limit:
                read_size = min(chunk_size, offset)
                offset -= read_size
                handle.seek(offset)
                chunk = handle.read(read_size)
                data[:0] = chunk
                line_breaks += chunk.count(b"\n")
    except Exception:
        return []
    text = bytes(data).decode("utf-8", errors="ignore")
    return text.splitlines()[-int(limit) :]
