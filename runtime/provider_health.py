from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config.config import PROJECT_ROOT


PROVIDERS = ("gecko", "pumpportal", "jupiter", "rugcheck", "dexscreener", "data_completeness")


def _scan_logs(root: Path) -> dict[str, int]:
    counts = {provider: 0 for provider in PROVIDERS}
    for path in (root / "logs").glob("*.txt"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:
            continue
        counts["gecko"] += text.count("gecko") + text.count("429")
        counts["pumpportal"] += text.count("pumpportal disconnect") + text.count("pumpportal error")
        counts["jupiter"] += text.count("no route") + text.count("jupiter")
        counts["rugcheck"] += text.count("rugcheck error") + text.count("rugcheck timeout")
        counts["dexscreener"] += text.count("dexscreener error") + text.count("dex screener error")
        counts["data_completeness"] += text.count("missing") + text.count("null")
    return counts


def provider_health_snapshot(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    counts = _scan_logs(root)
    providers: dict[str, Any] = {}
    for provider in PROVIDERS:
        count = counts.get(provider, 0)
        status = "ok"
        if count >= 500:
            status = "degraded"
        if count >= 2000:
            status = "critical"
        providers[provider] = {
            "status": status,
            "recent_error_signals": count,
            "paper_action": "annotate",
            "live_action": "shadow_or_reduce_size" if status != "ok" else "allow",
        }
    payload = {"providers": providers, "overall_status": "ok"}
    if any(item["status"] == "critical" for item in providers.values()):
        payload["overall_status"] = "critical"
    elif any(item["status"] == "degraded" for item in providers.values()):
        payload["overall_status"] = "degraded"
    metrics = root / "data" / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)
    (metrics / "provider_health.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


__all__ = ["PROVIDERS", "provider_health_snapshot"]
