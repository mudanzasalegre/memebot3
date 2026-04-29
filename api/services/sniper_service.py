from __future__ import annotations

from analytics.missed_pumps import build_missed_pumps
from analytics.sniper_audit import build_sniper_audit
from config.config import CFG
from runtime.hot_queue import GLOBAL_HOT_QUEUE
from runtime.live_canary import snapshot as live_canary_snapshot
from runtime.social_enrichment_queue import GLOBAL_SOCIAL_ENRICHMENT_QUEUE


def _green_sniper_policy() -> dict[str, object]:
    return {
        "enabled": bool(getattr(CFG, "GREEN_SNIPER_ENABLED", True)),
        "paper_sniper_mode": bool(getattr(CFG, "PAPER_SNIPER_MODE", False)),
        "live_enabled": bool(getattr(CFG, "GREEN_SNIPER_LIVE_ENABLED", False)),
        "entry_lane": "pump_early_green_candle_sniper",
        "require_route_paper": bool(getattr(CFG, "GREEN_SNIPER_REQUIRE_ROUTE_PAPER", False)),
        "require_route_live": bool(getattr(CFG, "GREEN_SNIPER_REQUIRE_ROUTE_LIVE", True)),
        "allow_proxy_liquidity_paper": bool(getattr(CFG, "GREEN_SNIPER_ALLOW_PROXY_LIQUIDITY_PAPER", True)),
        "rank_guard_enabled": bool(getattr(CFG, "GREEN_SNIPER_RANK_GUARD_ENABLED", True)),
        "rank_guard_min_score": float(getattr(CFG, "GREEN_SNIPER_RANK_GUARD_MIN_SCORE", 45.0) or 45.0),
        "rank_guard_bypass_paper_birth_probe": bool(
            getattr(CFG, "GREEN_SNIPER_RANK_GUARD_BYPASS_PAPER_BIRTH_PROBE", False)
        ),
        "paper_birth_probe": {
            "enabled": bool(getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_ENABLED", True)),
            "shadow_first": bool(getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_SHADOW_FIRST", True)),
            "max_age_min": float(getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_AGE_MIN", 3.0) or 3.0),
            "min_liquidity_usd": float(
                getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_MIN_LIQUIDITY_USD", 1000.0) or 1000.0
            ),
            "max_price_impact_pct": float(
                getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_PRICE_IMPACT_PCT", 25.0) or 25.0
            ),
        },
        "ml_mode": str(getattr(CFG, "GREEN_SNIPER_ML_MODE", "sizing_only") or "sizing_only"),
        "ml_can_block": bool(getattr(CFG, "GREEN_SNIPER_ML_BLOCK_ENABLED", False)),
        "socials": {
            "enabled": bool(getattr(CFG, "SOCIALS_ENABLED", True)),
            "async_only": bool(getattr(CFG, "SOCIALS_ASYNC_ONLY", True)),
            "hot_path_blocking": bool(getattr(CFG, "SOCIALS_HOT_PATH_BLOCKING", False)),
            "require_socials": bool(getattr(CFG, "GREEN_SNIPER_REQUIRE_SOCIALS", False)),
            "suspicious_can_block": bool(getattr(CFG, "GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_BLOCK", False)),
        },
        "paper_size_sol": {
            "micro": float(getattr(CFG, "GREEN_SNIPER_SIZE_MICRO_SOL", 0.10) or 0.10),
            "core": float(getattr(CFG, "GREEN_SNIPER_SIZE_CORE_SOL", 0.10) or 0.10),
            "hot": float(getattr(CFG, "GREEN_SNIPER_SIZE_HOT_SOL", 0.10) or 0.10),
        },
        "live_size_sol": float(getattr(CFG, "GREEN_SNIPER_LIVE_SIZE_SOL", 0.01) or 0.01),
        "research_rank_canary": {
            "enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_LIVE_ENABLED", False)),
            "min_score": float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_SCORE", 61.15) or 61.15),
        },
        "risk_guards": {
            "green_sniper_risk_guard_enabled": bool(getattr(CFG, "GREEN_SNIPER_RISK_GUARD_ENABLED", True)),
            "liquidity_guard_enabled": bool(getattr(CFG, "GREEN_SNIPER_LIQ_GUARD_ENABLED", True)),
            "early_dump_enabled": bool(getattr(CFG, "GREEN_SNIPER_EARLY_DUMP_ENABLED", True)),
            "late_momentum_watch_enabled": bool(getattr(CFG, "LATE_MOMENTUM_WATCH_ENABLED", True)),
        },
    }


def sniper_status() -> dict[str, object]:
    audit = build_sniper_audit()
    hot = GLOBAL_HOT_QUEUE.snapshot()
    return {
        "hot_queue_size": hot["size"],
        "hot_queue": hot,
        "green_sniper_buys_today": audit.get("bought_by_lane", {}).get("pump_early_green_candle_sniper", 0),
        "green_sniper_shadows_today": audit.get("shadowed_by_reason", {}),
        "green_sniper_rejects_today": audit.get("rejected_by_reason", {}),
        "avg_time_to_eval_s": audit.get("avg_time_seen_to_eval_s"),
        "avg_time_to_buy_s": audit.get("avg_time_seen_to_buy_s"),
        "top_reject_reasons": list(audit.get("rejected_by_reason", {}).items())[:10],
        "missed_pumps_top10": build_missed_pumps()[:10],
        "live_canary": live_canary_snapshot(),
        "green_sniper_policy": _green_sniper_policy(),
        "social_enrichment": GLOBAL_SOCIAL_ENRICHMENT_QUEUE.snapshot(),
    }


def missed_pumps(limit: int = 50) -> dict[str, object]:
    rows = build_missed_pumps()[: max(1, min(int(limit), 250))]
    return {"count": len(rows), "items": rows}


def hot_queue_status() -> dict[str, object]:
    return GLOBAL_HOT_QUEUE.snapshot()


__all__ = ["hot_queue_status", "missed_pumps", "sniper_status"]
