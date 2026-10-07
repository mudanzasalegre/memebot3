from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from config.config import PROJECT_ROOT
from ml.lane_taxonomy import (
    LANE_MOONSHOT_MICRO_LOTTERY,
    LANE_PAPER_BOOTSTRAP_MICRO,
    LANE_PAPER_EXPLORATION_MICRO,
    LANE_RESEARCH_RANK_CANARY,
    LANE_SHADOW_FOLLOWUP_MICRO,
    LANE_SNIPER_RESEARCH_MICRO_FALLBACK,
    normalize_entry_lane,
)


AUTOTUNE_STATE_JSON = "current_run_autotune_state.json"
MANUAL_LANE_CONTROLS_JSON = "manual_lane_controls.json"
DEFAULT_COOLDOWN_MIN = 60.0
LIVE_KEYS = {
    "LIVE_CANARY_ENABLED",
    "GREEN_SNIPER_LIVE_ENABLED",
    "RESEARCH_RANK_CANARY_LIVE_ENABLED",
    "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED",
    "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED",
    "SNIPER_RESEARCH_MICRO_FALLBACK_LIVE_ENABLED",
    "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED",
    "LATE_MOMENTUM_WATCH_LIVE_ENABLED",
    "AUTO_PROMOTE_LIVE",
    "MODEL_AUTO_PROMOTE",
    "ML_AUTO_PROMOTE_LANES",
    "LLM_TRADING_ENABLED",
}

LANE_NAME_TO_CANONICAL = {
    "paper_bootstrap": LANE_PAPER_BOOTSTRAP_MICRO,
    "paper_exploration": LANE_PAPER_EXPLORATION_MICRO,
    "shadow_followup_micro": LANE_SHADOW_FOLLOWUP_MICRO,
    "moonshot_micro_lottery": LANE_MOONSHOT_MICRO_LOTTERY,
    "research_rank_canary": LANE_RESEARCH_RANK_CANARY,
    "sniper_research_micro_fallback": LANE_SNIPER_RESEARCH_MICRO_FALLBACK,
}

DISABLE_KEY_TO_LANES = {
    "PAPER_BOOTSTRAP_ENABLED": {LANE_PAPER_BOOTSTRAP_MICRO},
    "PAPER_EXPLORATION_QUOTA_ENABLED": {LANE_PAPER_EXPLORATION_MICRO},
    "PAPER_IDLE_MICRO_EXPLORATION_ENABLED": {LANE_PAPER_EXPLORATION_MICRO},
    "SHADOW_FOLLOWUP_MICRO_ENABLED": {LANE_SHADOW_FOLLOWUP_MICRO},
    "MOONSHOT_MICRO_LOTTERY_ENABLED": {LANE_MOONSHOT_MICRO_LOTTERY},
    "SNIPER_RESEARCH_MICRO_FALLBACK_ENABLED": {LANE_SNIPER_RESEARCH_MICRO_FALLBACK},
    "RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED": {LANE_RESEARCH_RANK_CANARY},
    "RESEARCH_RANK_CANARY_PAPER_NORMAL_BUY_ENABLED": {LANE_RESEARCH_RANK_CANARY},
}


@dataclass(frozen=True)
class PolicyOverlayDecision:
    allowed: bool
    lane: str
    reason: str = "ok"
    cooldown_until: str | None = None
    backoff_s: int = 0
    source: str = "policy_overlay"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        out = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            out = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=dt.timezone.utc)
    return out.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def _lane_from_value(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("entry_lane") or value.get("lane") or value.get("profit_lane_tier")
    normalized = normalize_entry_lane(value)
    return LANE_NAME_TO_CANONICAL.get(normalized, normalized)


def _lane_from_action(action: dict[str, Any]) -> str:
    raw = str(action.get("lane") or "").strip()
    return LANE_NAME_TO_CANONICAL.get(raw, normalize_entry_lane(raw))


def _cooldown_until(generated_at: dt.datetime, cooldown_min: float) -> str:
    return _iso(generated_at + dt.timedelta(minutes=max(float(cooldown_min or 0.0), 0.0)))


def _change_blocks(change_key: str, value: Any) -> bool:
    if change_key in LIVE_KEYS:
        return False
    if change_key not in DISABLE_KEY_TO_LANES:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false", "no", "n", "off"}
    return value is False


def build_policy_overlay_state(
    autotune_state: dict[str, Any],
    *,
    now: dt.datetime | None = None,
    cooldown_min: float = DEFAULT_COOLDOWN_MIN,
    enabled: bool = True,
) -> dict[str, Any]:
    generated = parse_time(autotune_state.get("generated_at_utc")) or now or utc_now()
    blocked_by_lane: dict[str, dict[str, Any]] = {}

    changes = autotune_state.get("recommended_changes") or {}
    if isinstance(changes, dict):
        for key, value in sorted(changes.items()):
            if not _change_blocks(str(key), value):
                continue
            for lane in DISABLE_KEY_TO_LANES[str(key)]:
                blocked_by_lane.setdefault(
                    lane,
                    {
                        "lane": lane,
                        "reason": f"recommended_change:{key}=false",
                        "action": "recommended_change",
                        "change_key": key,
                        "cooldown_until": _cooldown_until(generated, cooldown_min),
                    },
                )

    actions = autotune_state.get("actions") or []
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_name = str(action.get("action") or "")
            if action_name not in {
                "disable_severe_loss_lane",
                "disable_negative_expectancy_lane",
                "cooldown_toxic_exit_lane",
                "carry_forward_lane_cooldown",
                "rank_canary_shadow_only",
            }:
                continue
            lane = _lane_from_action(action)
            if not lane:
                continue
            blocked_by_lane[lane] = {
                "lane": lane,
                "reason": str(action.get("reason") or action_name),
                "action": action_name,
                "cooldown_until": str(action.get("cooldown_until") or _cooldown_until(generated, cooldown_min)),
            }

    return {
        "enabled": bool(enabled),
        "mode": "paper_only_runtime_overlay",
        "cooldown_min": float(cooldown_min),
        "generated_at_utc": _iso(generated),
        "blocked_lanes": list(blocked_by_lane.values()),
        "live_guarded": True,
        "live_keys_guarded": sorted(LIVE_KEYS),
        "source": AUTOTUNE_STATE_JSON,
    }


def _metrics_dir(root: Path | None = None) -> Path:
    return (root or PROJECT_ROOT) / "data" / "metrics"


def _manual_controls_path(root: Path | None = None) -> Path:
    return _metrics_dir(root) / MANUAL_LANE_CONTROLS_JSON


def load_autotune_state(root: Path | None = None) -> dict[str, Any]:
    path = _metrics_dir(root) / AUTOTUNE_STATE_JSON
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_manual_lane_controls(root: Path | None = None) -> dict[str, Any]:
    path = _manual_controls_path(root)
    if not path.exists():
        return {"lanes": {}, "source": MANUAL_LANE_CONTROLS_JSON}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {"lanes": {}, "source": MANUAL_LANE_CONTROLS_JSON, "load_error": "invalid_json"}
    if not isinstance(payload, dict):
        return {"lanes": {}, "source": MANUAL_LANE_CONTROLS_JSON, "load_error": "invalid_payload"}
    lanes = payload.get("lanes")
    if not isinstance(lanes, dict):
        payload["lanes"] = {}
    payload.setdefault("source", MANUAL_LANE_CONTROLS_JSON)
    return payload


def write_manual_lane_controls(state: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    path = _manual_controls_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lanes = state.get("lanes")
    state["lanes"] = lanes if isinstance(lanes, dict) else {}
    state["source"] = MANUAL_LANE_CONTROLS_JSON
    state.setdefault("generated_at_utc", _iso(utc_now()))
    state["updated_at_utc"] = _iso(utc_now())
    path.write_text(json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    return state


def set_manual_lane_control(
    lane: Any,
    *,
    disabled: bool,
    root: Path | None = None,
    reason: Any = "manual_operator_control",
    requested_by: Any = "control_command",
    command_id: Any = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    normalized_lane = _lane_from_value(lane)
    if not normalized_lane or normalized_lane == "unknown":
        raise ValueError(f"invalid lane: {lane}")
    state = load_manual_lane_controls(root)
    lanes = state.setdefault("lanes", {})
    if not isinstance(lanes, dict):
        lanes = {}
        state["lanes"] = lanes
    current = lanes.get(normalized_lane) if isinstance(lanes.get(normalized_lane), dict) else {}
    current_disabled = bool(current.get("disabled")) if current else False
    changed = current_disabled != bool(disabled)
    updated = {
        **current,
        "lane": normalized_lane,
        "disabled": bool(disabled),
        "reason": str(reason or "manual_operator_control")[:240],
        "updated_at_utc": _iso(now or utc_now()),
        "updated_by": str(requested_by or "control_command")[:120],
        "source_command_id": int(command_id) if command_id not in (None, "") else current.get("source_command_id"),
        "changed": changed,
    }
    if not disabled:
        updated["reason"] = str(reason or "manual_operator_reenabled")[:240]
    lanes[normalized_lane] = updated
    write_manual_lane_controls(state, root)
    return {
        "lane": normalized_lane,
        "disabled": bool(disabled),
        "changed": changed,
        "state": updated,
        "path": str(_manual_controls_path(root)),
    }


def manual_lane_blocks(manual_state: dict[str, Any] | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    state = manual_state if manual_state is not None else load_manual_lane_controls(root)
    lanes = state.get("lanes") if isinstance(state, dict) else {}
    if not isinstance(lanes, dict):
        return []
    blocks: list[dict[str, Any]] = []
    for lane, entry in sorted(lanes.items()):
        if not isinstance(entry, dict) or not bool(entry.get("disabled")):
            continue
        normalized = _lane_from_value(entry.get("lane") or lane)
        if not normalized or normalized == "unknown":
            continue
        blocks.append(
            {
                "lane": normalized,
                "reason": str(entry.get("reason") or "manual_operator_control"),
                "action": "manual_disable_lane",
                "cooldown_until": None,
                "updated_at_utc": entry.get("updated_at_utc"),
                "updated_by": entry.get("updated_by"),
                "source": MANUAL_LANE_CONTROLS_JSON,
            }
        )
    return blocks


def overlay_from_autotune_state(
    autotune_state: dict[str, Any],
    *,
    now: dt.datetime | None = None,
    cooldown_min: float = DEFAULT_COOLDOWN_MIN,
    enabled: bool = True,
) -> dict[str, Any]:
    runtime_overlay = autotune_state.get("runtime_overlay")
    if isinstance(runtime_overlay, dict):
        return runtime_overlay
    return build_policy_overlay_state(
        autotune_state,
        now=now,
        cooldown_min=cooldown_min,
        enabled=enabled,
    )


def _active_blocks(blocks: Iterable[dict[str, Any]], *, now: dt.datetime) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for block in blocks:
        until = parse_time(block.get("cooldown_until"))
        if until is not None and until <= now:
            continue
        active.append(block)
    return active


def evaluate_policy_overlay(
    token_or_lane: Any,
    *,
    dry_run: bool,
    live: bool,
    root: Path | None = None,
    autotune_state: dict[str, Any] | None = None,
    overlay_state: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
    cooldown_min: float = DEFAULT_COOLDOWN_MIN,
    enabled: bool = True,
) -> PolicyOverlayDecision:
    lane = _lane_from_value(token_or_lane)
    if live or not dry_run:
        return PolicyOverlayDecision(True, lane, "live_guard_no_runtime_overlay")
    current = now or utc_now()
    state = autotune_state if autotune_state is not None else load_autotune_state(root)
    overlay = overlay_state if overlay_state is not None else overlay_from_autotune_state(
        state,
        now=current,
        cooldown_min=cooldown_min,
        enabled=enabled,
    )
    manual_blocks = manual_lane_blocks(root=root)
    if (not overlay or not bool(overlay.get("enabled", True))) and not manual_blocks:
        return PolicyOverlayDecision(True, lane, "overlay_disabled")
    blocks = list((overlay or {}).get("blocked_lanes") or [])
    blocks.extend(manual_blocks)
    for block in _active_blocks(blocks, now=current):
        if normalize_entry_lane(block.get("lane")) != lane:
            continue
        until_raw = block.get("cooldown_until")
        until = parse_time(until_raw)
        backoff_s = int(max(60.0, (until - current).total_seconds())) if until else 300
        return PolicyOverlayDecision(
            False,
            lane,
            f"policy_overlay:{block.get('action') or 'block'}:{block.get('reason') or 'lane_blocked'}",
            cooldown_until=str(until_raw or ""),
            backoff_s=backoff_s,
        )
    return PolicyOverlayDecision(True, lane, "ok")


__all__ = [
    "AUTOTUNE_STATE_JSON",
    "DEFAULT_COOLDOWN_MIN",
    "DISABLE_KEY_TO_LANES",
    "LIVE_KEYS",
    "MANUAL_LANE_CONTROLS_JSON",
    "PolicyOverlayDecision",
    "build_policy_overlay_state",
    "evaluate_policy_overlay",
    "load_autotune_state",
    "load_manual_lane_controls",
    "manual_lane_blocks",
    "overlay_from_autotune_state",
    "parse_time",
    "set_manual_lane_control",
    "write_manual_lane_controls",
]
