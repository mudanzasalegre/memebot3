from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from research_loop.paths import metrics_dir, project_root, research_runs_dir

PROVIDERS = (
    "DexScreener",
    "GeckoTerminal",
    "Birdeye",
    "Jupiter",
    "PumpFun",
    "RugCheck",
    "Helius",
    "RPC",
)

BUDGET_METRIC_KEYS = (
    "gecko_429_count",
    "birdeye_404_count",
    "birdeye_429_count",
    "jupiter_rate_limit_count",
    "pumpfun_disconnect_count",
    "rpc_errors",
    "cooldown_count",
    "provider_degraded_minutes",
)


@dataclass(frozen=True)
class ApiBudgetComparison:
    ok: bool
    deltas: dict[str, float] = field(default_factory=dict)
    rejection_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "deltas": dict(self.deltas),
            "rejection_reasons": list(self.rejection_reasons),
            "warnings": list(self.warnings),
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _iter_log_lines(root: Path) -> Iterable[str]:
    log_dir = root / "logs"
    if not log_dir.exists():
        return []
    lines: list[str] = []
    for path in sorted(log_dir.glob("*.txt")):
        try:
            lines.extend(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        except Exception:
            continue
    return lines


_RATE_LIMIT_RE = re.compile(r"\b(?:rate[\s_-]*limit(?:ed|ing)?|too\s+many\s+requests)\b", re.IGNORECASE)
_DISCONNECT_RE = re.compile(
    r"\b(?:disconnect(?:ed|ion)?|desconectad[oa]s?|desconexi[oó]n)\b",
    re.IGNORECASE,
)
_ERROR_RE = re.compile(
    r"\b(?:error|exception|timeout|timed\s+out|fail(?:ed|ure)?|unavailable)\b",
    re.IGNORECASE,
)
_REQUEST_RE = re.compile(
    r"\b(?:provider_request|request_sent|http_request|sending\s+(?:get|post)|(?:get|post)\s+https?://)",
    re.IGNORECASE,
)
_PROVIDER_HEALTH_RE = re.compile(
    r"(?:\bprovider(?:[_\s]+health)?\b.{0,80}\b(?:degraded|critical)\b|"
    r"\b(?:degraded|critical)\b.{0,80}\bprovider(?:[_\s]+health)?\b)",
    re.IGNORECASE,
)
_OPERATIONAL_EVENT_RE = re.compile(
    r"(?:provider|http|api|request|response|rate_limit|disconnect|cooldown|health|error)",
    re.IGNORECASE,
)


def _has_explicit_http_status(text: str, status_code: int) -> bool:
    """Match an HTTP status signal, not the same digits in a mint/timestamp/price."""

    return bool(
        re.search(
            rf"\b(?:http(?:\s+status)?|status(?:[_\s-]+code)?|response(?:\s+status)?|code)"
            rf"\s*[:=#-]?\s*{status_code}\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _event_status_code(row: dict[str, Any]) -> int | None:
    for key in ("status_code", "http_status", "http_status_code", "response_status", "error_code"):
        value = row.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            status = int(value)
            if status == value and 100 <= status <= 599:
                return status
            continue
        match = re.fullmatch(
            r"\s*(?:(?:http|status|code)\s*[:=#-]?\s*)?(\d{3})\s*",
            str(value),
            flags=re.IGNORECASE,
        )
        if match:
            status = int(match.group(1))
            if 100 <= status <= 599:
                return status
    return None


def _event_message(row: dict[str, Any]) -> str:
    values = [
        row.get(key)
        for key in ("message", "error", "error_message", "reason", "detail", "state", "health_status", "status")
    ]
    return " ".join(str(value) for value in values if value is not None).lower()


def _event_identity(row: dict[str, Any]) -> tuple[str, str] | None:
    for key in ("event_id", "request_id", "trace_id", "decision_id"):
        value = row.get(key)
        if value not in (None, ""):
            return key, str(value)
    return None


def _line_provider(text: str) -> str | None:
    lowered = text.lower()
    if "dexscreener" in lowered or "dex screener" in lowered:
        return "DexScreener"
    if "gecko" in lowered or "[gt]" in lowered:
        return "GeckoTerminal"
    if "birdeye" in lowered:
        return "Birdeye"
    if "jupiter" in lowered:
        return "Jupiter"
    if "pumpportal" in lowered or "pumpfun" in lowered or "pump.fun" in lowered:
        return "PumpFun"
    if "rugcheck" in lowered:
        return "RugCheck"
    if "helius" in lowered:
        return "Helius"
    if "rpc" in lowered or "jsonrpc" in lowered:
        return "RPC"
    return None


def _increment_estimate(estimates: dict[str, int], provider: str | None) -> None:
    if provider:
        estimates[provider] = estimates.get(provider, 0) + 1


def _scan_text_line(line: str, counts: dict[str, int], estimates: dict[str, int]) -> None:
    text = line.lower()
    provider = _line_provider(text)
    if provider and _REQUEST_RE.search(text):
        _increment_estimate(estimates, provider)

    is_rate_limited = _has_explicit_http_status(text, 429) or bool(_RATE_LIMIT_RE.search(text))
    if provider == "GeckoTerminal" and is_rate_limited:
        counts["gecko_429_count"] += 1
    if provider == "Birdeye" and _has_explicit_http_status(text, 404):
        counts["birdeye_404_count"] += 1
    if provider == "Birdeye" and is_rate_limited:
        counts["birdeye_429_count"] += 1
    if provider == "Jupiter" and is_rate_limited:
        counts["jupiter_rate_limit_count"] += 1
    if provider == "PumpFun" and _DISCONNECT_RE.search(text):
        counts["pumpfun_disconnect_count"] += 1
    if provider in {"RPC", "Helius"} and (is_rate_limited or _ERROR_RE.search(text)):
        counts["rpc_errors"] += 1
    if provider and re.search(r"\bprovider(?:[_\s]+cooldown)?\b.{0,80}\bcooldown\b|\bcooldown\b.{0,80}\bprovider\b", text):
        counts["cooldown_count"] += 1
    if _PROVIDER_HEALTH_RE.search(text):
        counts["provider_degraded_minutes"] += 1


def _scan_event(row: dict[str, Any], counts: dict[str, int], estimates: dict[str, int]) -> None:
    event_type = str(row.get("event_type") or row.get("type") or "").strip().lower()
    operational = bool(_OPERATIONAL_EVENT_RE.search(event_type))

    provider: str | None = None
    for key in ("provider", "provider_name", "service", "api"):
        value = row.get(key)
        if value not in (None, ""):
            provider = _provider_from_value(str(value)) or _line_provider(str(value))
            if provider:
                break
    if provider is None and operational:
        for key in ("source", "price_source"):
            value = row.get(key)
            if value not in (None, ""):
                provider = _provider_from_value(str(value)) or _line_provider(str(value))
                if provider:
                    break

    # Candidate snapshots frequently contain source=jupiter plus arbitrary 429
    # digits. Only operational events or an explicit provider field are valid
    # API-budget evidence.
    if not operational and not any(row.get(key) not in (None, "") for key in ("provider", "provider_name", "service", "api")):
        return

    if provider and re.search(r"(?:^|_)(?:provider|api|http)?_?request(?:_|$)|request_(?:sent|started|completed)", event_type):
        _increment_estimate(estimates, provider)

    status_code = _event_status_code(row)
    message = _event_message(row)
    is_rate_limited = status_code == 429 or bool(_RATE_LIMIT_RE.search(message))

    if provider == "GeckoTerminal" and is_rate_limited:
        counts["gecko_429_count"] += 1
    if provider == "Birdeye" and status_code == 404:
        counts["birdeye_404_count"] += 1
    if provider == "Birdeye" and is_rate_limited:
        counts["birdeye_429_count"] += 1
    if provider == "Jupiter" and is_rate_limited:
        counts["jupiter_rate_limit_count"] += 1
    if provider == "PumpFun" and (_DISCONNECT_RE.search(message) or "disconnect" in event_type):
        counts["pumpfun_disconnect_count"] += 1
    if provider in {"RPC", "Helius"} and (
        is_rate_limited or (status_code is not None and status_code >= 400) or _ERROR_RE.search(message)
    ):
        counts["rpc_errors"] += 1
    if provider and ("cooldown" in event_type or re.search(r"\bprovider\b.{0,80}\bcooldown\b", message)):
        counts["cooldown_count"] += 1
    if (
        "provider_degraded" in event_type
        or "provider_health" in event_type and re.search(r"\b(?:degraded|critical)\b", message)
        or _PROVIDER_HEALTH_RE.search(message)
    ):
        counts["provider_degraded_minutes"] += 1


def _provider_from_value(value: str) -> str | None:
    lowered = re.sub(r"[^a-z0-9]+", "", value.lower())
    aliases = {
        "dex": "DexScreener",
        "dexscreener": "DexScreener",
        "gt": "GeckoTerminal",
        "gecko": "GeckoTerminal",
        "geckoterminal": "GeckoTerminal",
        "birdeye": "Birdeye",
        "jupiter": "Jupiter",
        "pumpfun": "PumpFun",
        "pumpportal": "PumpFun",
        "rugcheck": "RugCheck",
        "helius": "Helius",
        "rpc": "RPC",
        "jsonrpc": "RPC",
    }
    return aliases.get(lowered)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def build_api_budget_report(root: str | Path | None = None, *, write: bool = True) -> dict[str, Any]:
    resolved_root = project_root(root)
    counts = {key: 0 for key in BUDGET_METRIC_KEYS}
    estimates = {provider: 0 for provider in PROVIDERS}
    # runtime_events is the canonical operational telemetry source. The
    # decision ledger and candidate outcomes mirror many of the same decisions
    # and are intentionally excluded to avoid multiplying one provider signal.
    event_paths = (metrics_dir(resolved_root) / "runtime_events.jsonl",)
    ignored_mirror_paths = (
        metrics_dir(resolved_root) / "decision_ledger.jsonl",
        metrics_dir(resolved_root) / "candidate_outcomes.jsonl",
    )

    rows_scanned = 0
    duplicate_events_skipped = 0
    seen_event_ids: set[tuple[str, str]] = set()
    for path in event_paths:
        rows = _read_jsonl(path)
        rows_scanned += len(rows)
        for row in rows:
            identity = _event_identity(row)
            if identity is not None:
                if identity in seen_event_ids:
                    duplicate_events_skipped += 1
                    continue
                seen_event_ids.add(identity)
            _scan_event(row, counts, estimates)

    log_lines = list(_iter_log_lines(resolved_root))
    for line in log_lines:
        _scan_text_line(line, counts, estimates)

    payload: dict[str, Any] = {
        "generated_at_utc": utc_now(),
        **counts,
        "estimated_requests_by_provider": estimates,
        "sources": {
            "event_files": [str(path.relative_to(resolved_root)) for path in event_paths if path.exists()],
            "ignored_mirror_files": [
                str(path.relative_to(resolved_root)) for path in ignored_mirror_paths if path.exists()
            ],
            "rows_scanned": rows_scanned,
            "duplicate_events_skipped": duplicate_events_skipped,
            "log_lines_scanned": len(log_lines),
            "mode": "local_files_only",
            "detection_version": "strict_provider_signals_v2",
            "request_estimate_method": "explicit_request_events_and_log_markers_only",
            "provider_degraded_metric_semantics": "explicit_degraded_signal_count_legacy_key",
        },
    }
    if write:
        _write_json(research_runs_dir(resolved_root) / "api_budget.json", payload)
        _write_json(metrics_dir(resolved_root) / "api_budget_report.json", payload)
    return payload


def _metric(payload: dict[str, Any], key: str) -> float:
    try:
        return float(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def compare_api_budget(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    provider_degraded_tolerance_minutes: float = 0.0,
) -> ApiBudgetComparison:
    deltas = {key: _metric(candidate, key) - _metric(baseline, key) for key in BUDGET_METRIC_KEYS}
    rejection_reasons: list[str] = []
    warnings: list[str] = []

    total_429_delta = deltas["gecko_429_count"] + deltas["birdeye_429_count"] + deltas["jupiter_rate_limit_count"]
    deltas["api_429_count"] = total_429_delta
    if total_429_delta > 0:
        rejection_reasons.append("api_budget:api_429_count_delta>0")
    if deltas["provider_degraded_minutes"] > provider_degraded_tolerance_minutes:
        rejection_reasons.append("api_budget:provider_degraded_minutes_delta_exceeds_tolerance")

    baseline_estimates = baseline.get("estimated_requests_by_provider") or {}
    candidate_estimates = candidate.get("estimated_requests_by_provider") or {}
    if isinstance(baseline_estimates, dict) and isinstance(candidate_estimates, dict):
        for provider in PROVIDERS:
            deltas[f"estimated_requests_by_provider.{provider}"] = (
                _metric(candidate_estimates, provider) - _metric(baseline_estimates, provider)
            )
    else:
        warnings.append("missing_estimated_requests_by_provider")

    return ApiBudgetComparison(
        ok=not rejection_reasons,
        deltas=deltas,
        rejection_reasons=rejection_reasons,
        warnings=warnings,
    )


def metrics_from_api_budget(payload: dict[str, Any]) -> dict[str, float]:
    metrics = {key: _metric(payload, key) for key in BUDGET_METRIC_KEYS}
    metrics["api_429_count"] = (
        metrics["gecko_429_count"] + metrics["birdeye_429_count"] + metrics["jupiter_rate_limit_count"]
    )
    return metrics


__all__ = [
    "ApiBudgetComparison",
    "BUDGET_METRIC_KEYS",
    "PROVIDERS",
    "build_api_budget_report",
    "compare_api_budget",
    "metrics_from_api_budget",
]
