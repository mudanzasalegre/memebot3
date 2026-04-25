from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Any

import pandas as pd

from api.schemas.common import Envelope, MetaPayload, SourceStatus

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore


UTC = dt.timezone.utc


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, pd.Timestamp):
        ts = value
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.isoformat()
    if isinstance(value, dt.datetime):
        ts = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return ts.astimezone(UTC).isoformat()
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min, tzinfo=UTC).isoformat()
    return None


def to_jsonable(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, pd.Timestamp):
        return iso_or_none(value)

    if isinstance(value, dt.datetime):
        return iso_or_none(value)

    if isinstance(value, dt.date):
        return iso_or_none(value)

    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value

    if np is not None and isinstance(value, np.generic):
        return to_jsonable(value.item())

    return value


def make_source_status(
    *,
    source_key: str,
    kind: str,
    status: str,
    updated_at: Any = None,
    detail: str | None = None,
    path: Path | str | None = None,
) -> SourceStatus:
    return SourceStatus(
        source_key=source_key,
        kind=kind,
        status=status,  # type: ignore[arg-type]
        updated_at=iso_or_none(updated_at) if updated_at is not None else None,
        detail=detail,
        path=str(path) if path is not None else None,
    )


def build_envelope(
    data: Any,
    *,
    source_status: list[SourceStatus],
    empty: bool = False,
    degraded: bool | None = None,
    stale: bool | None = None,
) -> Envelope:
    stale_flag = bool(stale) if stale is not None else any(item.status == "stale" for item in source_status)
    degraded_flag = bool(degraded) if degraded is not None else any(
        item.status in {"missing", "error"} for item in source_status
    )
    return Envelope(
        data=to_jsonable(data),
        meta=MetaPayload(
            generated_at=utc_now().isoformat(),
            degraded=degraded_flag,
            empty=bool(empty),
            stale=stale_flag,
            source_status=source_status,
        ),
    )
