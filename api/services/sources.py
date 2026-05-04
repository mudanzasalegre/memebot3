from __future__ import annotations

from pathlib import Path

from api.repositories.filesystem import (
    file_mtime,
    load_jsonl_rows,
    newest_matching_file,
    parse_timestamp,
    read_json_file,
)
from api.repositories.sqlite import table_row_counts
from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, iso_or_none, make_source_status
from api.settings import APISettings
from runtime.command_bus import ensure_control_commands_schema
from api.repositories.ui_saved_views import ensure_ui_saved_views_schema


CORE_TABLES = ("tokens", "positions", "revived_tokens")


def sqlite_main_status(settings: APISettings) -> SourceStatus:
    if not settings.db_path.exists():
        return make_source_status(
            source_key="sqlite.main",
            kind="sqlite",
            status="missing",
            detail="database_file_missing",
            path=settings.db_path,
        )

    try:
        counts = table_row_counts(settings.db_path, CORE_TABLES)
    except Exception as exc:
        return make_source_status(
            source_key="sqlite.main",
            kind="sqlite",
            status="error",
            detail=str(exc),
            path=settings.db_path,
        )

    detail = " ".join(f"{table}={counts.get(table)}" for table in CORE_TABLES)
    return make_source_status(
        source_key="sqlite.main",
        kind="sqlite",
        status="ok",
        detail=detail,
        path=settings.db_path,
    )


def sqlite_runtime_state_status(settings: APISettings) -> SourceStatus:
    if not settings.db_path.exists():
        return make_source_status(
            source_key="sqlite.bot_runtime_state",
            kind="sqlite",
            status="missing",
            detail="database_file_missing",
            path=settings.db_path,
        )

    try:
        counts = table_row_counts(settings.db_path, ("bot_runtime_state",))
    except Exception as exc:
        return make_source_status(
            source_key="sqlite.bot_runtime_state",
            kind="sqlite",
            status="error",
            detail=str(exc),
            path=settings.db_path,
        )

    rows = counts.get("bot_runtime_state")
    if rows is None:
        return make_source_status(
            source_key="sqlite.bot_runtime_state",
            kind="sqlite",
            status="missing",
            detail="table_missing",
            path=settings.db_path,
        )
    if rows == 0:
        return make_source_status(
            source_key="sqlite.bot_runtime_state",
            kind="sqlite",
            status="empty",
            detail="rows=0",
            path=settings.db_path,
        )
    return make_source_status(
        source_key="sqlite.bot_runtime_state",
        kind="sqlite",
        status="ok",
        detail=f"rows={rows}",
        path=settings.db_path,
    )


def sqlite_table_status(
    settings: APISettings,
    *,
    table: str,
    source_key: str | None = None,
) -> SourceStatus:
    if not settings.db_path.exists():
        return make_source_status(
            source_key=source_key or f"sqlite.{table}",
            kind="sqlite",
            status="missing",
            detail="database_file_missing",
            path=settings.db_path,
        )

    try:
        counts = table_row_counts(settings.db_path, (table,))
    except Exception as exc:
        return make_source_status(
            source_key=source_key or f"sqlite.{table}",
            kind="sqlite",
            status="error",
            detail=str(exc),
            path=settings.db_path,
        )

    rows = counts.get(table)
    if rows is None:
        return make_source_status(
            source_key=source_key or f"sqlite.{table}",
            kind="sqlite",
            status="missing",
            detail="table_missing",
            path=settings.db_path,
        )
    if rows == 0:
        return make_source_status(
            source_key=source_key or f"sqlite.{table}",
            kind="sqlite",
            status="empty",
            detail="rows=0",
            path=settings.db_path,
        )
    return make_source_status(
        source_key=source_key or f"sqlite.{table}",
        kind="sqlite",
        status="ok",
        detail=f"rows={rows}",
        path=settings.db_path,
    )


def jsonl_status(
    *,
    source_key: str,
    path: Path,
    optional: bool = False,
) -> SourceStatus:
    rows = load_jsonl_rows(path)
    if not path.exists():
        status = "empty" if optional else "missing"
        detail = "optional_missing" if optional else "file_missing"
        return make_source_status(source_key=source_key, kind="jsonl", status=status, detail=detail, path=path)

    if not rows:
        return make_source_status(
            source_key=source_key,
            kind="jsonl",
            status="empty",
            updated_at=file_mtime(path),
            detail="rows=0",
            path=path,
        )

    timestamps = [parsed for parsed in (parse_timestamp(row.get("ts_utc")) for row in rows) if parsed is not None]
    last_ts = max(timestamps, default=None)
    return make_source_status(
        source_key=source_key,
        kind="jsonl",
        status="ok",
        updated_at=last_ts or file_mtime(path),
        detail=f"rows={len(rows)}",
        path=path,
    )


def json_status(
    *,
    source_key: str,
    path: Path,
    generated_field: str | None = None,
    optional: bool = False,
    empty_when_missing: bool | None = None,
) -> SourceStatus:
    payload = read_json_file(path)
    missing_as_empty = bool(optional) if empty_when_missing is None else bool(empty_when_missing)
    if payload is None:
        status = "empty" if missing_as_empty else "missing"
        detail = "optional_missing" if missing_as_empty else "file_missing"
        return make_source_status(source_key=source_key, kind="json", status=status, detail=detail, path=path)

    updated_at = file_mtime(path)
    if isinstance(payload, dict) and generated_field and payload.get(generated_field) is not None:
        parsed = parse_timestamp(payload.get(generated_field))
        if parsed is not None:
            updated_at = parsed

    status = "empty" if payload in ({}, [], None) else "ok"
    return make_source_status(
        source_key=source_key,
        kind="json",
        status=status,
        updated_at=updated_at,
        detail="loaded",
        path=path,
    )


def latest_parquet_status(settings: APISettings) -> SourceStatus:
    latest = newest_matching_file(settings.features_dir, "features_*.parquet")
    if latest is None:
        return make_source_status(
            source_key="features.latest_parquet",
            kind="parquet",
            status="empty",
            detail="no_features_parquet",
            path=settings.features_dir,
        )

    return make_source_status(
        source_key="features.latest_parquet",
        kind="parquet",
        status="ok",
        updated_at=file_mtime(latest),
        detail=latest.name,
        path=latest,
    )


def paper_portfolio_status(settings: APISettings) -> SourceStatus:
    if not settings.paper_portfolio_path.exists():
        return make_source_status(
            source_key="paper.portfolio",
            kind="json",
            status="empty",
            detail="optional_missing",
            path=settings.paper_portfolio_path,
        )

    payload = read_json_file(settings.paper_portfolio_path)
    if payload in (None, {}, []):
        return make_source_status(
            source_key="paper.portfolio",
            kind="json",
            status="empty",
            updated_at=file_mtime(settings.paper_portfolio_path),
            detail="empty_portfolio",
            path=settings.paper_portfolio_path,
        )

    return make_source_status(
        source_key="paper.portfolio",
        kind="json",
        status="ok",
        updated_at=file_mtime(settings.paper_portfolio_path),
        detail="loaded",
        path=settings.paper_portfolio_path,
    )


def collect_source_status(settings: APISettings) -> list[SourceStatus]:
    ensure_control_commands_schema(settings.db_path)
    ensure_ui_saved_views_schema(settings.db_path)
    return [
        sqlite_main_status(settings),
        sqlite_runtime_state_status(settings),
        sqlite_table_status(settings, table="control_commands", source_key="sqlite.control_commands"),
        sqlite_table_status(settings, table="ui_saved_views", source_key="sqlite.ui_saved_views"),
        jsonl_status(source_key="metrics.runtime_events", path=settings.runtime_events_path),
        jsonl_status(source_key="metrics.research_events", path=settings.research_events_path),
        json_status(
            source_key="metrics.research_scorecard",
            path=settings.research_scorecard_json,
            generated_field="generated_at_utc",
            optional=True,
        ),
        json_status(
            source_key="metrics.research_thresholds",
            path=settings.research_thresholds_json,
            generated_field="generated_at_utc",
            optional=True,
        ),
        latest_parquet_status(settings),
        paper_portfolio_status(settings),
    ]


def get_sources_status_envelope(settings: APISettings) -> Envelope:
    statuses = collect_source_status(settings)
    data = {"sources": [item.model_dump() for item in statuses]}
    empty = all(item.status == "empty" for item in statuses)
    return build_envelope(data, source_status=statuses, empty=empty)
