from __future__ import annotations

import collections
import datetime as dt
from pathlib import Path
from typing import Any

from analytics.report_utils import boolish, fnum, load_deduped_positions
from api.repositories.filesystem import read_json_file
from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, make_source_status
from api.services.sources import json_status, sqlite_table_status
from api.settings import APISettings
from ml.lane_taxonomy import TRAINABLE_LANES, lane_group, normalize_entry_lane
from runtime.policy_overlay import (
    MANUAL_LANE_CONTROLS_JSON,
    load_autotune_state,
    load_manual_lane_controls,
    manual_lane_blocks,
    overlay_from_autotune_state,
)
from runtime.position_limits import evaluate_lane_position_limit
from runtime.provider_health import provider_health_snapshot


SEVERE_LOSS_PCT = -25.0
PNL_BASIS_GROSS_SPOT = "gross_spot"


def _json(path: Path) -> Any:
    return read_json_file(path)


def _closed(row: dict[str, Any]) -> bool:
    return boolish(row.get("closed"), False) or row.get("closed_at") is not None or row.get("exit_reason") is not None


def _lane(row: dict[str, Any]) -> str:
    return normalize_entry_lane(row.get("entry_lane") or row.get("lane") or row.get("profit_lane_tier"))


def _pnl_pct(row: dict[str, Any]) -> float:
    return fnum(row.get("total_pnl_pct") or row.get("realized_pnl_pct") or row.get("pnl_pct"), 0.0)


def _pnl_usd(row: dict[str, Any]) -> float:
    return fnum(row.get("total_pnl_usd") or row.get("realized_pnl_usd") or row.get("pnl_usd"), 0.0)


def _profit_factor(pnls: list[float]) -> float | None:
    gross_profit = sum(value for value in pnls if value > 0.0)
    gross_loss = abs(sum(value for value in pnls if value < 0.0))
    if gross_loss == 0.0:
        return None if gross_profit == 0.0 else 999.0
    return round(gross_profit / gross_loss, 6)


def _reason(row: dict[str, Any]) -> str:
    return str(row.get("exit_reason") or row.get("reason") or row.get("reject_reason") or "unknown").strip() or "unknown"


def _loss_reasons(closed_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in closed_rows:
        pnl_usd = _pnl_usd(row)
        pnl_pct = _pnl_pct(row)
        if pnl_usd >= 0.0 and pnl_pct >= 0.0:
            continue
        key = _reason(row)
        entry = grouped.setdefault(
            key,
            {
                "reason": key,
                "count": 0,
                "total_pnl_usd": 0.0,
                "avg_pnl_pct": 0.0,
                "severe_loss_count": 0,
            },
        )
        entry["count"] += 1
        entry["total_pnl_usd"] += pnl_usd
        entry["avg_pnl_pct"] += pnl_pct
        if pnl_pct <= SEVERE_LOSS_PCT:
            entry["severe_loss_count"] += 1
    out = []
    for entry in grouped.values():
        count = int(entry["count"] or 0)
        entry["total_pnl_usd"] = round(float(entry["total_pnl_usd"]), 6)
        entry["avg_pnl_pct"] = round(float(entry["avg_pnl_pct"]) / count, 6) if count else 0.0
        out.append(entry)
    return sorted(out, key=lambda item: (float(item["total_pnl_usd"]), -int(item["count"]), str(item["reason"])))[:10]


def _extract_missed_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("rows") or payload.get("data") or payload.get("items") or []
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def _missed_moonshot_reasons(metrics_dir: Path) -> list[dict[str, Any]]:
    rows = []
    rows.extend(_extract_missed_rows(_json(metrics_dir / "current_run_missed_pumps.json")))
    rows.extend(_extract_missed_rows(_json(metrics_dir / "missed_pumps.json")))
    filtered = []
    for row in rows:
        lane = _lane(row)
        peak = fnum(row.get("peak_pct") or row.get("later_max_pnl_pct") or row.get("observed_peak_after_seen"), 0.0)
        price_5m = fnum(row.get("price_pct_5m_at_seen") or row.get("price_pct_5m"), 0.0)
        if "moonshot" in lane or peak >= 100.0 or price_5m >= 100.0:
            filtered.append(row)
    counts = collections.Counter(_reason(row) for row in filtered)
    return [
        {"reason": reason, "count": count}
        for reason, count in counts.most_common(10)
    ]


def _manual_disabled_lanes(manual_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        normalize_entry_lane(block.get("lane")): block
        for block in manual_lane_blocks(manual_state=manual_state)
        if normalize_entry_lane(block.get("lane")) != "unknown"
    }


def _autotune_blocked_lanes(root: Path) -> dict[str, dict[str, Any]]:
    overlay = overlay_from_autotune_state(load_autotune_state(root))
    blocks = overlay.get("blocked_lanes") if isinstance(overlay, dict) else []
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(blocks, list):
        return out
    for block in blocks:
        if not isinstance(block, dict):
            continue
        lane = normalize_entry_lane(block.get("lane"))
        if lane != "unknown":
            out[lane] = dict(block)
    return out


def _lane_rows(
    *,
    positions: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    manual_disabled: dict[str, dict[str, Any]],
    autotune_disabled: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_lane = diagnostics.get("by_lane") if isinstance(diagnostics, dict) else {}
    by_lane = by_lane if isinstance(by_lane, dict) else {}
    all_lanes = set(TRAINABLE_LANES)
    all_lanes.update(normalize_entry_lane(lane) for lane in by_lane)
    all_lanes.update(manual_disabled)
    all_lanes.update(autotune_disabled)
    all_lanes.discard("unknown")

    open_positions = [row for row in positions if not _closed(row)]
    rows: list[dict[str, Any]] = []
    for lane in sorted(all_lanes):
        stats = by_lane.get(lane) if isinstance(by_lane.get(lane), dict) else {}
        cap = evaluate_lane_position_limit(lane, open_positions, dry_run=True, live=False)
        manual_block = manual_disabled.get(lane)
        autotune_block = autotune_disabled.get(lane)
        disabled_source = "manual" if manual_block else ("autotune" if autotune_block else None)
        rows.append(
            {
                "lane": lane,
                "group": lane_group(lane),
                "disabled": bool(disabled_source),
                "disabled_source": disabled_source,
                "manual_disabled": bool(manual_block),
                "autotune_blocked": bool(autotune_block),
                "disable_reason": (manual_block or autotune_block or {}).get("reason"),
                "cap": int(cap.cap),
                "open_count": int(cap.open_count),
                "cap_warning": "cap_zero" if int(cap.cap) == 0 else cap.warning,
                "pnl_rows": int(stats.get("pnl_rows") or 0),
                "avg_pnl_pct": stats.get("avg_pnl_pct"),
                "total_pnl_pct_points": stats.get("total_pnl_pct_points"),
                "severe_losses": int(stats.get("severe_losses") or 0),
                "buys": int(stats.get("buys") or 0),
                "shadows": int(stats.get("shadows") or 0),
                "missed_100": int(stats.get("peak_100") or 0),
                "policy_category": stats.get("policy_category"),
            }
        )
    return rows


def _top_lane_risks(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    risky = [
        row
        for row in lanes
        if int(row.get("severe_losses") or 0) > 0
        or float(row.get("total_pnl_pct_points") or 0.0) < 0.0
        or int(row.get("missed_100") or 0) > 0
    ]
    return sorted(
        risky,
        key=lambda row: (
            -int(row.get("severe_losses") or 0),
            float(row.get("total_pnl_pct_points") or 0.0),
            -int(row.get("missed_100") or 0),
            str(row.get("lane") or ""),
        ),
    )[:8]


def _manual_controls_status(settings: APISettings) -> SourceStatus:
    return json_status(
        source_key="metrics.manual_lane_controls",
        path=settings.metrics_dir / MANUAL_LANE_CONTROLS_JSON,
        generated_field="updated_at_utc",
        optional=True,
    )


def get_risk_control_envelope(settings: APISettings) -> Envelope:
    metrics_dir = settings.metrics_dir
    positions = load_deduped_positions(settings.project_root)
    closed_rows = [row for row in positions if _closed(row)]
    pnl_usd_values = [_pnl_usd(row) for row in closed_rows]
    pnl_pct_values = [_pnl_pct(row) for row in closed_rows]
    diagnostics = _json(metrics_dir / "current_run_trade_diagnostics.json")
    if not isinstance(diagnostics, dict):
        diagnostics = _json(metrics_dir / "trade_diagnostics.json")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    current_summary = _json(metrics_dir / "current_run_summary.json")
    current_summary = current_summary if isinstance(current_summary, dict) else {}
    manual_state = load_manual_lane_controls(settings.project_root)
    manual_disabled = _manual_disabled_lanes(manual_state)
    autotune_disabled = _autotune_blocked_lanes(settings.project_root)
    lanes = _lane_rows(
        positions=positions,
        diagnostics=diagnostics,
        manual_disabled=manual_disabled,
        autotune_disabled=autotune_disabled,
    )
    provider_health = provider_health_snapshot(settings.project_root)
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    gross_spot_pnl_usd = (
        fnum(current_summary.get("total_pnl_usd"), 0.0)
        if "total_pnl_usd" in current_summary
        else round(sum(pnl_usd_values), 6)
    )
    data = {
        "generated_at_utc": generated_at,
        "summary": {
            "closed_trades": len(closed_rows),
            "gross_spot_closed_pnl_usd": round(gross_spot_pnl_usd, 6),
            # Backward-compatible alias. This value is not net of network/priority fees.
            "net_closed_pnl_usd": round(gross_spot_pnl_usd, 6),
            "profit_factor": _profit_factor(pnl_usd_values),
            "severe_loss_count": sum(1 for value in pnl_pct_values if value <= SEVERE_LOSS_PCT),
            "manual_disabled_lanes": len(manual_disabled),
            "autotune_blocked_lanes": len(autotune_disabled),
            "cap_zero_lanes": sum(1 for row in lanes if int(row.get("cap") or 0) == 0),
            "provider_overall_status": provider_health.get("overall_status"),
        },
        "pnl_accounting": {
            "basis": PNL_BASIS_GROSS_SPOT,
            "canonical_field": "gross_spot_closed_pnl_usd",
            "fees_included": False,
            "network_fees_included": False,
            "priority_fees_included": False,
            "legacy_aliases": {
                "net_closed_pnl_usd": "gross_spot_closed_pnl_usd",
            },
        },
        "lanes": lanes,
        "top_lane_risks": _top_lane_risks(lanes),
        "top_loss_reasons": _loss_reasons(closed_rows),
        "top_missed_moonshot_reasons": _missed_moonshot_reasons(metrics_dir),
        "provider_health": provider_health,
        "manual_controls": manual_state,
    }
    statuses = [
        sqlite_table_status(settings, table="positions", source_key="sqlite.positions"),
        json_status(
            source_key="metrics.current_run_summary",
            path=metrics_dir / "current_run_summary.json",
            generated_field="generated_at_utc",
            optional=True,
        ),
        json_status(
            source_key="metrics.current_run_trade_diagnostics",
            path=metrics_dir / "current_run_trade_diagnostics.json",
            generated_field="generated_at_utc",
            optional=True,
        ),
        json_status(
            source_key="metrics.current_run_missed_pumps",
            path=metrics_dir / "current_run_missed_pumps.json",
            generated_field="generated_at_utc",
            optional=True,
        ),
        json_status(
            source_key="metrics.current_run_autotune_state",
            path=metrics_dir / "current_run_autotune_state.json",
            generated_field="generated_at_utc",
            optional=True,
        ),
        _manual_controls_status(settings),
        make_source_status(
            source_key="provider_health",
            kind="derived",
            status="ok",
            updated_at=generated_at,
            detail=str(provider_health.get("overall_status") or "unknown"),
            path=metrics_dir / "provider_health.json",
        ),
    ]
    return build_envelope(
        data,
        source_status=statuses,
        empty=not bool(lanes or closed_rows),
        degraded=any(item.status == "error" for item in statuses),
        stale=any(item.status == "stale" for item in statuses),
    )


__all__ = ["get_risk_control_envelope"]
