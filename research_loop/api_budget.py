from __future__ import annotations

import datetime as dt
import json
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


def _text_from_event(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, default=str).lower()


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
    _increment_estimate(estimates, provider)

    if "429" in text and ("gecko" in text or "[gt]" in text):
        counts["gecko_429_count"] += 1
    if "birdeye" in text and "404" in text:
        counts["birdeye_404_count"] += 1
    if "birdeye" in text and "429" in text:
        counts["birdeye_429_count"] += 1
    if "jupiter" in text and ("429" in text or "rate limit" in text or "too many requests" in text):
        counts["jupiter_rate_limit_count"] += 1
    if ("pumpfun" in text or "pumpportal" in text) and ("disconnect" in text or "disconnected" in text):
        counts["pumpfun_disconnect_count"] += 1
    if ("rpc" in text or "jsonrpc" in text or "helius" in text) and (
        "error" in text or "429" in text or "timeout" in text or "failed" in text
    ):
        counts["rpc_errors"] += 1
    if "cooldown" in text:
        counts["cooldown_count"] += 1
    if "degraded" in text or "provider health critical" in text or "provider_health critical" in text:
        counts["provider_degraded_minutes"] += 1


def _scan_event(row: dict[str, Any], counts: dict[str, int], estimates: dict[str, int]) -> None:
    text = _text_from_event(row)
    _scan_text_line(text, counts, estimates)

    provider = row.get("provider") or row.get("source") or row.get("price_source") or row.get("discovered_via")
    if provider:
        _increment_estimate(estimates, _line_provider(str(provider)) or _provider_from_value(str(provider)))


def _provider_from_value(value: str) -> str | None:
    lowered = value.lower()
    aliases = {
        "dex": "DexScreener",
        "dexscreener": "DexScreener",
        "gecko": "GeckoTerminal",
        "geckoterminal": "GeckoTerminal",
        "birdeye": "Birdeye",
        "jupiter": "Jupiter",
        "pumpfun": "PumpFun",
        "pumpportal": "PumpFun",
        "rugcheck": "RugCheck",
        "helius": "Helius",
        "rpc": "RPC",
    }
    return aliases.get(lowered)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def build_api_budget_report(root: str | Path | None = None, *, write: bool = True) -> dict[str, Any]:
    resolved_root = project_root(root)
    counts = {key: 0 for key in BUDGET_METRIC_KEYS}
    estimates = {provider: 0 for provider in PROVIDERS}
    event_paths = (
        metrics_dir(resolved_root) / "runtime_events.jsonl",
        metrics_dir(resolved_root) / "decision_ledger.jsonl",
        metrics_dir(resolved_root) / "candidate_outcomes.jsonl",
    )

    rows_scanned = 0
    for path in event_paths:
        rows = _read_jsonl(path)
        rows_scanned += len(rows)
        for row in rows:
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
            "rows_scanned": rows_scanned,
            "log_lines_scanned": len(log_lines),
            "mode": "local_files_only",
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
