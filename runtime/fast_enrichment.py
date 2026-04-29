from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class FastEnrichmentResult:
    level: int
    token: dict[str, Any]
    missing: tuple[str, ...]
    usable_for_green_sniper: bool

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["missing"] = list(self.missing)
        return out


def _has_value(token: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = token.get(key)
        if value is None:
            continue
        try:
            if float(value) == 0.0 and key not in {"price_pct_5m"}:
                continue
        except Exception:
            pass
        if str(value).strip() != "":
            return True
    return False


def enrich_fast(token: dict[str, Any]) -> FastEnrichmentResult:
    tok = dict(token or {})
    missing: list[str] = []
    address_ok = _has_value(tok, "address", "mint")
    if not address_ok:
        missing.append("address")
    level = 0 if address_ok else -1

    level1_fields = {
        "price": _has_value(tok, "price_usd", "price", "price_pct_5m"),
        "liquidity": _has_value(tok, "liquidity_usd"),
        "market_cap": _has_value(tok, "market_cap_usd", "mcap"),
        "txns": _has_value(tok, "txns_last_5m"),
    }
    for key, ok in level1_fields.items():
        if not ok:
            missing.append(key)
    if address_ok and any(level1_fields.values()):
        level = 1

    level2_fields = {
        "rug": _has_value(tok, "rug_score"),
        "socials": _has_value(tok, "social_ok"),
        "holders": _has_value(tok, "holders"),
        "trend": _has_value(tok, "trend"),
    }
    for key, ok in level2_fields.items():
        if not ok:
            missing.append(key)
    if address_ok and all(level1_fields.values()) and all(level2_fields.values()):
        level = 2

    tok["fast_enrichment_level"] = level
    tok["fast_enrichment_missing"] = ",".join(missing)
    usable = level >= 1 and (level1_fields["price"] or level1_fields["txns"])
    return FastEnrichmentResult(level=level, token=tok, missing=tuple(missing), usable_for_green_sniper=usable)


__all__ = ["FastEnrichmentResult", "enrich_fast"]
