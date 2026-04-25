from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from api.repositories.filesystem import file_mtime, newest_matching_file, tail_text_lines
from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, make_source_status
from api.services.sources import jsonl_status
from api.settings import APISettings


def _resolve_target(settings: APISettings, target: str) -> tuple[str, Path, str, str]:
    target_clean = str(target or "app").strip()
    if target_clean in {"", "app"}:
        latest = newest_matching_file(settings.logs_dir, "*.txt")
        if latest is None:
            return "app", settings.logs_dir / "app.log", "file", "logs.app"
        return "app", latest, "file", "logs.app"

    if target_clean == "runtime_events":
        return target_clean, settings.runtime_events_path, "jsonl", "metrics.runtime_events"
    if target_clean == "research_events":
        return target_clean, settings.research_events_path, "jsonl", "metrics.research_events"

    if Path(target_clean).name != target_clean:
        raise HTTPException(status_code=404, detail="Invalid log target")

    candidate = (settings.logs_dir / target_clean).resolve()
    try:
        candidate.relative_to(settings.logs_dir)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Invalid log target") from exc

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Log target not found")
    return target_clean, candidate, "file", f"logs.{candidate.name}"


def _file_status(*, source_key: str, path: Path) -> SourceStatus:
    if not path.exists():
        return make_source_status(
            source_key=source_key,
            kind="file",
            status="missing",
            detail="file_missing",
            path=path,
        )
    return make_source_status(
        source_key=source_key,
        kind="file",
        status="ok",
        updated_at=file_mtime(path),
        detail=path.name,
        path=path,
    )


def get_logs_tail_envelope(
    settings: APISettings,
    *,
    target: str = "app",
    lines: int = 200,
) -> Envelope:
    resolved_target, path, kind, source_key = _resolve_target(settings, target)
    text_lines = tail_text_lines(path, limit=lines)

    if kind == "jsonl":
        status = jsonl_status(source_key=source_key, path=path)
    else:
        status = _file_status(source_key=source_key, path=path)
        if not text_lines:
            status = status.model_copy(update={"status": "empty", "detail": f"{path.name} lines=0"})
        else:
            status = status.model_copy(update={"detail": f"{path.name} lines={len(text_lines)}"})

    data = {
        "target": resolved_target,
        "path": str(path),
        "lines": text_lines,
        "count": len(text_lines),
    }
    return build_envelope(
        data,
        source_status=[status],
        empty=not text_lines,
        degraded=status.status in {"missing", "error"},
        stale=status.status == "stale",
    )
