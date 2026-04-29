from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config.config import CFG, PROJECT_ROOT
from utils.time import utc_now


SOCIAL_STATUS_UNKNOWN = "unknown"
SOCIAL_STATUS_MISSING = "missing"
SOCIAL_STATUS_PRESENT = "present"
SOCIAL_STATUS_SUSPICIOUS = "suspicious"
SOCIAL_STATUSES = {
    SOCIAL_STATUS_UNKNOWN,
    SOCIAL_STATUS_MISSING,
    SOCIAL_STATUS_PRESENT,
    SOCIAL_STATUS_SUSPICIOUS,
}

SOCIAL_ENRICHMENT_EVENTS_PATH = PROJECT_ROOT / "data" / "metrics" / "social_enrichment.jsonl"
SOCIAL_LINKS_SEEN_PATH = PROJECT_ROOT / "data" / "metrics" / "social_links_seen.jsonl"


@dataclass(frozen=True)
class SocialSignal:
    status: str
    social_ok: bool | None
    twitter_present: bool
    telegram_present: bool
    discord_present: bool
    website_present: bool
    link_count: int
    confidence_bonus: float
    risk_flags: tuple[str, ...]
    source: str
    latency_ms: int | None = None
    twitter_url: str | None = None
    telegram_url: str | None = None
    discord_url: str | None = None
    website_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["risk_flags"] = list(self.risk_flags)
        return payload


def _clean_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        raw = f"https://twitter.com/{raw[1:]}"
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw
    if not parsed.netloc:
        return raw
    return parsed.geturl().rstrip("/")


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return None


def _normalize_status(value: Any) -> str:
    status = str(value or SOCIAL_STATUS_UNKNOWN).strip().lower()
    return status if status in SOCIAL_STATUSES else SOCIAL_STATUS_UNKNOWN


def _links_from_profile(profile: dict[str, Any]) -> dict[str, str | None]:
    links: dict[str, str | None] = {
        "twitter": None,
        "telegram": None,
        "discord": None,
        "website": None,
    }

    raw_links = profile.get("links")
    if isinstance(raw_links, dict):
        candidates = {
            "twitter": raw_links.get("twitterUrl") or raw_links.get("twitter") or raw_links.get("x"),
            "telegram": raw_links.get("telegramUrl") or raw_links.get("telegram"),
            "discord": raw_links.get("discordUrl") or raw_links.get("discord"),
            "website": raw_links.get("website") or raw_links.get("websiteUrl") or raw_links.get("url"),
        }
        for key, value in candidates.items():
            links[key] = _clean_url(value)
    elif isinstance(raw_links, list):
        for item in raw_links:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or item.get("label") or item.get("name") or "").lower()
            url = _clean_url(item.get("url") or item.get("value"))
            if not url:
                continue
            if "twitter" in kind or kind == "x":
                links["twitter"] = links["twitter"] or url
            elif "telegram" in kind or kind == "tg":
                links["telegram"] = links["telegram"] or url
            elif "discord" in kind:
                links["discord"] = links["discord"] or url
            elif "website" in kind or "web" in kind:
                links["website"] = links["website"] or url

    for key, value in (
        ("twitter", profile.get("twitterUrl") or profile.get("twitter")),
        ("telegram", profile.get("telegramUrl") or profile.get("telegram")),
        ("discord", profile.get("discordUrl") or profile.get("discord")),
        ("website", profile.get("website") or profile.get("websiteUrl")),
    ):
        links[key] = links[key] or _clean_url(value)

    return links


def social_signal_from_profile(
    profile: dict[str, Any] | None,
    *,
    source: str = "dexscreener",
    latency_ms: int | None = None,
) -> SocialSignal:
    if not profile:
        return unknown_social_signal(source=source, latency_ms=latency_ms)

    links = _links_from_profile(profile)
    twitter = links["twitter"]
    telegram = links["telegram"]
    discord = links["discord"]
    website = links["website"]
    link_count = sum(1 for value in (twitter, telegram, discord, website) if value)
    status = SOCIAL_STATUS_PRESENT if link_count else SOCIAL_STATUS_MISSING
    bonus = float(getattr(CFG, "GREEN_SNIPER_SOCIALS_SCORE_BONUS", 5.0) or 5.0) if link_count else 0.0
    return SocialSignal(
        status=status,
        social_ok=bool(link_count) if status != SOCIAL_STATUS_UNKNOWN else None,
        twitter_present=bool(twitter),
        telegram_present=bool(telegram),
        discord_present=bool(discord),
        website_present=bool(website),
        link_count=link_count,
        confidence_bonus=bonus,
        risk_flags=(),
        source=source,
        latency_ms=latency_ms,
        twitter_url=twitter,
        telegram_url=telegram,
        discord_url=discord,
        website_url=website,
    )


def unknown_social_signal(*, source: str = "unknown", latency_ms: int | None = None) -> SocialSignal:
    return SocialSignal(
        status=SOCIAL_STATUS_UNKNOWN,
        social_ok=None,
        twitter_present=False,
        telegram_present=False,
        discord_present=False,
        website_present=False,
        link_count=0,
        confidence_bonus=0.0,
        risk_flags=(),
        source=source,
        latency_ms=latency_ms,
    )


def social_signal_from_token(token: dict[str, Any]) -> SocialSignal:
    raw_signal = token.get("social_signal")
    if isinstance(raw_signal, SocialSignal):
        return raw_signal
    if isinstance(raw_signal, dict):
        return social_signal_from_dict(raw_signal)

    status = _normalize_status(token.get("social_status"))
    if status == SOCIAL_STATUS_UNKNOWN and token.get("social_ok") is not None:
        status = SOCIAL_STATUS_PRESENT if bool(token.get("social_ok")) else SOCIAL_STATUS_MISSING

    links = {
        "twitter": _clean_url(token.get("twitter_url")),
        "telegram": _clean_url(token.get("telegram_url")),
        "discord": _clean_url(token.get("discord_url")),
        "website": _clean_url(token.get("website_url")),
    }
    link_count = int(token.get("social_link_count") or sum(1 for value in links.values() if value))
    risk_flags = token.get("social_risk_flags") or ()
    if isinstance(risk_flags, str):
        risk_flags = tuple(item.strip() for item in risk_flags.split(",") if item.strip())
    return SocialSignal(
        status=status,
        social_ok=bool(token.get("social_ok")) if token.get("social_ok") is not None else None,
        twitter_present=bool(token.get("twitter_present") or links["twitter"]),
        telegram_present=bool(token.get("telegram_present") or links["telegram"]),
        discord_present=bool(token.get("discord_present") or links["discord"]),
        website_present=bool(token.get("website_present") or links["website"]),
        link_count=link_count,
        confidence_bonus=float(token.get("social_confidence_bonus") or 0.0),
        risk_flags=tuple(risk_flags),
        source=str(token.get("social_source") or "token"),
        latency_ms=int(token["social_latency_ms"]) if token.get("social_latency_ms") is not None else None,
        twitter_url=links["twitter"],
        telegram_url=links["telegram"],
        discord_url=links["discord"],
        website_url=links["website"],
    )


def social_signal_from_dict(payload: dict[str, Any]) -> SocialSignal:
    risk_flags = payload.get("risk_flags") or payload.get("social_risk_flags") or ()
    if isinstance(risk_flags, str):
        risk_flags = tuple(item.strip() for item in risk_flags.split(",") if item.strip())
    return SocialSignal(
        status=_normalize_status(payload.get("status") or payload.get("social_status")),
        social_ok=payload.get("social_ok") if payload.get("social_ok") is None else bool(payload.get("social_ok")),
        twitter_present=bool(payload.get("twitter_present")),
        telegram_present=bool(payload.get("telegram_present")),
        discord_present=bool(payload.get("discord_present")),
        website_present=bool(payload.get("website_present")),
        link_count=int(payload.get("link_count") or payload.get("social_link_count") or 0),
        confidence_bonus=float(payload.get("confidence_bonus") or payload.get("social_confidence_bonus") or 0.0),
        risk_flags=tuple(str(item) for item in risk_flags),
        source=str(payload.get("source") or payload.get("social_source") or "unknown"),
        latency_ms=int(payload["latency_ms"]) if payload.get("latency_ms") is not None else None,
        twitter_url=_clean_url(payload.get("twitter_url")),
        telegram_url=_clean_url(payload.get("telegram_url")),
        discord_url=_clean_url(payload.get("discord_url")),
        website_url=_clean_url(payload.get("website_url")),
    )


def apply_social_signal_to_token(token: dict[str, Any], signal: SocialSignal) -> dict[str, Any]:
    token["social_signal"] = signal.to_dict()
    token["social_status"] = signal.status
    token["social_ok"] = signal.social_ok
    token["twitter_present"] = int(signal.twitter_present)
    token["telegram_present"] = int(signal.telegram_present)
    token["discord_present"] = int(signal.discord_present)
    token["website_present"] = int(signal.website_present)
    token["social_link_count"] = int(signal.link_count)
    token["social_confidence_bonus"] = float(signal.confidence_bonus)
    token["social_risk_flags"] = ",".join(signal.risk_flags)
    token["social_latency_ms"] = signal.latency_ms
    token["twitter_url"] = signal.twitter_url
    token["telegram_url"] = signal.telegram_url
    token["discord_url"] = signal.discord_url
    token["website_url"] = signal.website_url
    return token


def _iter_links(signal: SocialSignal) -> list[str]:
    return [
        item
        for item in (signal.twitter_url, signal.telegram_url, signal.discord_url, signal.website_url)
        if item
    ]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def flag_suspicious_links(
    signal: SocialSignal,
    *,
    address: str | None = None,
    symbol: str | None = None,
    history_path: Path = SOCIAL_LINKS_SEEN_PATH,
) -> SocialSignal:
    if signal.status not in {SOCIAL_STATUS_PRESENT, SOCIAL_STATUS_SUSPICIOUS}:
        return signal
    if not bool(getattr(CFG, "SOCIALS_SUSPICIOUS_ENABLED", True)):
        return signal

    max_tokens = int(getattr(CFG, "SOCIALS_REUSED_LINK_MAX_TOKENS", 3) or 3)
    rows = _read_jsonl(history_path)
    risk_flags = set(signal.risk_flags)
    current_links = _iter_links(signal)
    current_domains = {_domain(link) for link in current_links if _domain(link)}

    for link in current_links:
        addresses = {
            str(row.get("address") or "")
            for row in rows
            if str(row.get("url") or "").rstrip("/") == link.rstrip("/")
        }
        addresses.discard(str(address or ""))
        if len(addresses) >= max_tokens:
            risk_flags.add("reused_link")

    for domain in current_domains:
        addresses = {
            str(row.get("address") or "")
            for row in rows
            if str(row.get("domain") or "") == domain
        }
        addresses.discard(str(address or ""))
        if len(addresses) >= max_tokens:
            risk_flags.add("reused_domain")

    sym = str(symbol or "").strip().lower()
    if sym and signal.website_url:
        domain = _domain(signal.website_url) or ""
        if len(sym) >= 4 and sym not in domain:
            risk_flags.add("website_symbol_mismatch")

    if not risk_flags:
        return signal
    return replace(
        signal,
        status=SOCIAL_STATUS_SUSPICIOUS,
        risk_flags=tuple(sorted(risk_flags)),
        confidence_bonus=max(0.0, signal.confidence_bonus - float(getattr(CFG, "GREEN_SNIPER_SOCIALS_RISK_PENALTY", 5.0) or 5.0)),
    )


def record_social_links(
    signal: SocialSignal,
    *,
    address: str | None,
    symbol: str | None = None,
    path: Path = SOCIAL_LINKS_SEEN_PATH,
) -> None:
    links = _iter_links(signal)
    if not links:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = utc_now().isoformat()
    with path.open("a", encoding="utf-8") as fh:
        for url in links:
            fh.write(
                json.dumps(
                    {
                        "ts_utc": ts,
                        "address": address,
                        "symbol": symbol,
                        "url": url,
                        "domain": _domain(url),
                        "source": signal.source,
                    },
                    ensure_ascii=True,
                )
                + "\n"
            )


def record_social_signal(
    signal: SocialSignal,
    *,
    address: str,
    symbol: str | None = None,
    lane: str | None = None,
    path: Path = SOCIAL_ENRICHMENT_EVENTS_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts_utc": utc_now().isoformat(),
        "address": address,
        "symbol": symbol,
        "lane": lane,
        **signal.to_dict(),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=True) + "\n")


def latest_social_signal(address: str, *, path: Path = SOCIAL_ENRICHMENT_EVENTS_PATH) -> SocialSignal | None:
    rows = _read_jsonl(path)
    for row in reversed(rows):
        if str(row.get("address") or "") == str(address):
            return social_signal_from_dict(row)
    return None


def latest_social_payload(address: str) -> dict[str, Any] | None:
    signal = latest_social_signal(address)
    return signal.to_dict() if signal else None


__all__ = [
    "SOCIAL_ENRICHMENT_EVENTS_PATH",
    "SOCIAL_LINKS_SEEN_PATH",
    "SOCIAL_STATUS_MISSING",
    "SOCIAL_STATUS_PRESENT",
    "SOCIAL_STATUS_SUSPICIOUS",
    "SOCIAL_STATUS_UNKNOWN",
    "SocialSignal",
    "apply_social_signal_to_token",
    "flag_suspicious_links",
    "latest_social_payload",
    "latest_social_signal",
    "record_social_links",
    "record_social_signal",
    "social_signal_from_dict",
    "social_signal_from_profile",
    "social_signal_from_token",
    "unknown_social_signal",
]
