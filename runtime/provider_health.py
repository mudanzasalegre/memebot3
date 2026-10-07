from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from config.config import PROJECT_ROOT


PROVIDERS = (
    "gecko",
    "birdeye",
    "pumpportal",
    "jupiter",
    "rugcheck",
    "dexscreener",
    "data_completeness",
)

_PROVIDER_ALIASES: dict[str, tuple[re.Pattern[str], ...]] = {
    "gecko": (
        re.compile(r"\bgecko(?:terminal)?\b", re.IGNORECASE),
        re.compile(r"\[gt\]", re.IGNORECASE),
    ),
    "birdeye": (re.compile(r"\bbirdeye\b", re.IGNORECASE),),
    "pumpportal": (
        re.compile(r"\bpumpportal\b", re.IGNORECASE),
        re.compile(r"\bpumpfun\b", re.IGNORECASE),
        re.compile(r"\bpump\.fun\b", re.IGNORECASE),
    ),
    "jupiter": (re.compile(r"\bjupiter\b", re.IGNORECASE),),
    "rugcheck": (re.compile(r"\brugcheck\b", re.IGNORECASE),),
    "dexscreener": (
        re.compile(r"\bdexscreener\b", re.IGNORECASE),
        re.compile(r"\bdex\s+screener\b", re.IGNORECASE),
    ),
}
_HTTP_ERROR_RE = re.compile(
    r"\b(?:http(?:\s+status)?|status(?:[_\s-]+code)?|response(?:\s+status)?|code)"
    r"\s*[:=#-]?\s*[45]\d{2}\b",
    re.IGNORECASE,
)
_RATE_LIMIT_RE = re.compile(r"\b(?:rate[\s_-]*limit(?:ed|ing)?|too\s+many\s+requests)\b", re.IGNORECASE)
_FAILURE_RE = re.compile(
    r"\b(?:error|exception|timeout|timed\s+out|fail(?:ed|ure)?|unavailable)\b",
    re.IGNORECASE,
)
_DISCONNECT_RE = re.compile(
    r"\b(?:disconnect(?:ed|ion)?|desconectad[oa]s?|desconexi[oó]n)\b",
    re.IGNORECASE,
)
_NO_RESULT_RE = re.compile(
    r"\b(?:no|sin)\s+(?:price|precio|response|respuesta|data|datos)\b.{0,60}\b(?:attempts?|intentos?)\b",
    re.IGNORECASE,
)
_DATA_COMPLETENESS_RE = re.compile(
    r"(?:\bcampos?\s+cr[ií]ticos?\s+nulos?\b|\bmissing\s+required\s+(?:data|fields?)\b|"
    r"\b(?:data\s+)?completeness\b.{0,60}\b(?:missing|incomplete|invalid)\b|"
    r"\b(?:incomplete|missing)\s+(?:market|token|candidate)\s+data\b)",
    re.IGNORECASE,
)


def _provider_has_error_signal(line: str, provider: str) -> bool:
    aliases = _PROVIDER_ALIASES[provider]
    if not any(pattern.search(line) for pattern in aliases):
        return False
    return bool(
        _HTTP_ERROR_RE.search(line)
        or _RATE_LIMIT_RE.search(line)
        or _FAILURE_RE.search(line)
        or _NO_RESULT_RE.search(line)
        or (provider == "pumpportal" and _DISCONNECT_RE.search(line))
    )


def _scan_logs(root: Path) -> dict[str, int]:
    counts = {provider: 0 for provider in PROVIDERS}
    for path in (root / "logs").glob("*.txt"):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for line in lines:
            for provider in _PROVIDER_ALIASES:
                if _provider_has_error_signal(line, provider):
                    # One log record is one signal even if it contains several
                    # failure words (for example "HTTP 429 rate limited").
                    counts[provider] += 1
            if _DATA_COMPLETENESS_RE.search(line):
                counts["data_completeness"] += 1
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
    payload = {
        "providers": providers,
        "overall_status": "ok",
        "sources": {
            "mode": "local_log_lines_only",
            "detection_version": "strict_provider_error_signals_v2",
        },
    }
    if any(item["status"] == "critical" for item in providers.values()):
        payload["overall_status"] = "critical"
    elif any(item["status"] == "degraded" for item in providers.values()):
        payload["overall_status"] = "degraded"
    metrics = root / "data" / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)
    (metrics / "provider_health.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


__all__ = ["PROVIDERS", "provider_health_snapshot"]
