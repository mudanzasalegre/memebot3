from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import math
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlparse

from config.config import CFG, PROJECT_ROOT
from utils.time import utc_now
from utils.numeric_types import binary_value


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
SOCIAL_RECEIPT_VERSION = "social_http_receipt_v1"
SOCIAL_RECEIPT_BASIS = "http_response_received_not_provider_profile_asof"
SOCIAL_FAILURE_BASIS = "request_failed_not_profile_observed"
SOCIAL_MAX_AGE_S = 600.0


@dataclass(frozen=True)
class SocialSignal:
    status: str
    social_ok: bool | None
    twitter_present: bool | None
    telegram_present: bool | None
    discord_present: bool | None
    website_present: bool | None
    link_count: int
    confidence_bonus: float
    risk_flags: tuple[str, ...]
    source: str
    latency_ms: int | None = None
    twitter_url: str | None = None
    telegram_url: str | None = None
    discord_url: str | None = None
    website_url: str | None = None
    address: str | None = None
    received_at: float | None = None
    receipt_version: str | None = None
    basis: str | None = None
    risk_checked_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["risk_flags"] = list(self.risk_flags)
        return payload


def _clean_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(c.isspace() or ord(c) < 32 for c in raw):
        return None
    if raw.startswith("@"):
        return None  # Handle resolution is platform-specific, not a generic URL.
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        parsed.port  # Reject malformed ports, too.
    except ValueError:
        return None
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


def _social_kind(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return {"x": "twitter", "twitter": "twitter", "telegram": "telegram", "tg": "telegram",
            "discord": "discord", "website": "website", "web": "website"}.get(value.strip().lower())


def _handle_url(kind: str | None, handle: Any) -> str | None:
    if not isinstance(handle, str):
        return None
    if "://" in handle:
        return _clean_url(handle)
    name = handle.removeprefix("@")
    # Only platforms with an unambiguous public handle URL. A Discord username
    # is not an invite; never invent one or guess an unknown platform's URL.
    if kind == "twitter" and re.fullmatch(r"[A-Za-z0-9_]{1,15}", name):
        return f"https://x.com/{name}"
    if kind == "telegram" and re.fullmatch(r"[A-Za-z0-9_]{5,32}", name):
        return f"https://t.me/{name}"
    return None


def _profile_links(profile: dict[str, Any]) -> dict[str, str | None] | None:
    """Parse known flat/profile and pair.info variants, not arbitrary objects."""
    if "info" not in profile:
        raw_links = profile.get("links")
        if not any(key in profile for key in ("links", "twitterUrl", "twitter", "telegramUrl",
                                             "telegram", "discordUrl", "discord", "website", "websiteUrl")):
            return None
        if raw_links is not None and not isinstance(raw_links, (dict, list)):
            return None
        if isinstance(raw_links, list) and any(not isinstance(item, dict) for item in raw_links):
            return None
        candidates = [profile]
        if isinstance(raw_links, dict):
            candidates.append(raw_links)
        for candidate in candidates:
            for key in ("twitterUrl", "twitter", "x", "telegramUrl", "telegram", "discordUrl",
                        "discord", "website", "websiteUrl"):
                if candidate.get(key) not in (None, "") and _clean_url(candidate[key]) is None:
                    return None
        if isinstance(raw_links, list):
            for item in raw_links:
                if _social_kind(item.get("type") or item.get("label")) is not None and _clean_url(item.get("url")) is None:
                    return None
        return _links_from_profile(profile)
    info = profile.get("info")
    if not isinstance(info, dict) or not any(key in info for key in ("websites", "socials")):
        return None
    links = dict.fromkeys(("twitter", "telegram", "discord", "website"))
    for name in ("websites", "socials"):
        values = info.get(name, [])
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            return None
        for item in values:
            kind = "website" if name == "websites" else _social_kind(item.get("type") or item.get("platform"))
            if kind is None:
                continue
            url = _clean_url(item.get("url")) or _handle_url(kind, item.get("handle"))
            if url is None:
                return None  # A broken known entry is not proof of absence.
            links[kind] = links[kind] or url
    return links


def social_signal_from_profile(
    profile: dict[str, Any] | None,
    *,
    source: str = "dexscreener",
    latency_ms: int | None = None,
    address: str | None = None,
    received_at: float | None = None,
) -> SocialSignal:
    if not isinstance(profile, dict) or not profile:
        return unknown_social_signal(source=source, latency_ms=latency_ms, address=address,
                                     received_at=received_at, basis=SOCIAL_RECEIPT_BASIS)

    links = _profile_links(profile)
    if links is None:
        return unknown_social_signal(source=source, latency_ms=latency_ms, address=address,
                                     received_at=received_at, basis=SOCIAL_RECEIPT_BASIS)
    twitter = links["twitter"]
    telegram = links["telegram"]
    discord = links["discord"]
    website = links["website"]
    link_count = sum(1 for value in (twitter, telegram, discord, website) if value)
    info = profile.get("info")
    social_observed = not isinstance(info, dict) or "socials" in info
    website_observed = not isinstance(info, dict) or "websites" in info
    if not link_count and not (social_observed and website_observed):
        return unknown_social_signal(source=source, latency_ms=latency_ms, address=address,
                                     received_at=received_at, basis=SOCIAL_RECEIPT_BASIS)
    status = SOCIAL_STATUS_PRESENT if link_count else SOCIAL_STATUS_MISSING
    bonus = _finite(getattr(CFG, "GREEN_SNIPER_SOCIALS_SCORE_BONUS", 5.0), 0.) if link_count else 0.0
    return SocialSignal(
        status=status,
        social_ok=bool(link_count) if status != SOCIAL_STATUS_UNKNOWN else None,
        twitter_present=bool(twitter) if social_observed else None,
        telegram_present=bool(telegram) if social_observed else None,
        discord_present=bool(discord) if social_observed else None,
        website_present=bool(website) if website_observed else None,
        link_count=link_count,
        confidence_bonus=bonus,
        risk_flags=(),
        source=source,
        latency_ms=latency_ms,
        twitter_url=twitter,
        telegram_url=telegram,
        discord_url=discord,
        website_url=website,
        address=address,
        received_at=received_at,
        receipt_version=SOCIAL_RECEIPT_VERSION if received_at is not None else None,
        basis=SOCIAL_RECEIPT_BASIS if received_at is not None else None,
    )


def unknown_social_signal(*, source: str = "unknown", latency_ms: int | None = None,
                          address: str | None = None, received_at: float | None = None,
                          basis: str = SOCIAL_FAILURE_BASIS) -> SocialSignal:
    return SocialSignal(
        status=SOCIAL_STATUS_UNKNOWN,
        social_ok=None,
        twitter_present=None,
        telegram_present=None,
        discord_present=None,
        website_present=None,
        link_count=0,
        confidence_bonus=0.0,
        risk_flags=(),
        source=source,
        latency_ms=latency_ms,
        address=address,
        received_at=received_at,
        receipt_version=SOCIAL_RECEIPT_VERSION if received_at is not None else None,
        basis=basis if received_at is not None else None,
    )


def social_signal_from_token(token: dict[str, Any]) -> SocialSignal:
    raw_signal = token.get("social_signal")
    if isinstance(raw_signal, SocialSignal):
        return social_signal_from_dict(raw_signal.to_dict())
    if isinstance(raw_signal, dict):
        return social_signal_from_dict(raw_signal)
    return social_signal_from_dict({**token, "status": token.get("social_status"),
        "source": token.get("social_source") or "token",
        "link_count": token.get("social_link_count"),
        "confidence_bonus": token.get("social_confidence_bonus"),
        "risk_flags": token.get("social_risk_flags"), "latency_ms": token.get("social_latency_ms"),
        "address": token.get("social_address"), "received_at": token.get("social_received_at"),
        "receipt_version": token.get("social_receipt_version"), "basis": token.get("social_basis"),
        "risk_checked_at": token.get("social_risk_checked_at")})


def _count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return int(number) if math.isfinite(number) and number.is_integer() and 0 <= number <= 2**31 - 1 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _finite(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def social_signal_from_dict(payload: dict[str, Any]) -> SocialSignal:
    source = payload.get("source") or payload.get("social_source") or "unknown"
    source = source if isinstance(source, str) else "unknown"
    status = _normalize_status(payload.get("status") or payload.get("social_status"))
    okay = binary_value(payload.get("social_ok"))
    if payload.get("social_ok") is not None and okay is None:
        return unknown_social_signal(source=source)
    if status == SOCIAL_STATUS_UNKNOWN and okay is not None:
        status = SOCIAL_STATUS_PRESENT if okay else SOCIAL_STATUS_MISSING
    if (status == SOCIAL_STATUS_MISSING and okay == 1
            or status in {SOCIAL_STATUS_PRESENT, SOCIAL_STATUS_SUSPICIOUS} and okay == 0):
        return unknown_social_signal(source=source)
    risk_flags = payload.get("risk_flags") or payload.get("social_risk_flags") or ()
    if isinstance(risk_flags, str):
        risk_flags = tuple(item.strip() for item in risk_flags.split(",") if item.strip())
    if not isinstance(risk_flags, (list, tuple)) or any(not isinstance(item, str) for item in risk_flags):
        return unknown_social_signal(source=source)
    links = {kind: _clean_url(payload.get(f"{kind}_url")) for kind in ("twitter", "telegram", "discord", "website")}
    flags = {}
    for kind, link in links.items():
        flag = binary_value(payload.get(f"{kind}_present"))
        if payload.get(f"{kind}_present") is not None and flag is None:
            return unknown_social_signal(source=source)
        flags[kind] = (None if f"{kind}_present" in payload and payload[f"{kind}_present"] is None
                       else bool(flag) if flag is not None else bool(link))
    raw_count = payload.get("link_count")
    if raw_count is None:
        raw_count = payload.get("social_link_count")
    count = _count(raw_count) if raw_count is not None else sum(flag is True for flag in flags.values())
    raw_bonus = payload.get("confidence_bonus") if "confidence_bonus" in payload else payload.get("social_confidence_bonus")
    bonus = _finite(raw_bonus, 0. if raw_bonus is None else None)
    if count is None or bonus is None:
        return unknown_social_signal(source=source)
    return SocialSignal(
        status=status, social_ok=None if okay is None else bool(okay),
        twitter_present=flags["twitter"], telegram_present=flags["telegram"],
        discord_present=flags["discord"], website_present=flags["website"],
        link_count=count, confidence_bonus=bonus, risk_flags=tuple(risk_flags), source=source,
        latency_ms=_count(payload.get("latency_ms")), **{f"{kind}_url": link for kind, link in links.items()},
        address=payload.get("address") if isinstance(payload.get("address"), str) else None,
        received_at=_finite(payload.get("received_at")),
        receipt_version=payload.get("receipt_version"), basis=payload.get("basis"),
        risk_checked_at=_finite(payload.get("risk_checked_at")),
    )


def checked_social_receipt(raw: Any, address: str, *, now: float | None = None,
                           max_age_s: float = SOCIAL_MAX_AGE_S) -> SocialSignal | None:
    """Detached original HTTP observation; cache access never renews its clock."""
    if isinstance(raw, SocialSignal):
        raw = raw.to_dict()
    if not isinstance(raw, dict):
        return None
    if (any(raw.get(field) is not None and type(raw.get(field)) is not bool
            for field in ("twitter_present", "telegram_present", "discord_present", "website_present"))
            or raw.get("social_ok") is not None and type(raw["social_ok"]) is not bool
            or type(raw.get("link_count")) is not int
            or type(raw.get("received_at")) not in (int, float)
            or type(raw.get("confidence_bonus")) not in (int, float)
            or not isinstance(raw.get("basis"), str)
            or not isinstance(raw.get("receipt_version"), str)
            or not isinstance(raw.get("source"), str)):
        return None
    signal = social_signal_from_dict(raw)
    if (set(raw) != set(signal.to_dict()) or raw != signal.to_dict()
            or signal.address != address or not address or not signal.source
            or signal.receipt_version != SOCIAL_RECEIPT_VERSION or signal.received_at is None
            or signal.received_at <= 0 or signal.basis not in {SOCIAL_RECEIPT_BASIS, SOCIAL_FAILURE_BASIS}
            or signal.basis == SOCIAL_FAILURE_BASIS and signal.status != SOCIAL_STATUS_UNKNOWN):
        return None
    if (now is not None and type(now) not in (int, float) or type(max_age_s) not in (int, float)):
        return None
    age = (time.time() if now is None else now) - signal.received_at
    if (isinstance(max_age_s, bool) or not math.isfinite(age) or not math.isfinite(max_age_s)
            or not 0 < max_age_s <= SOCIAL_MAX_AGE_S or not -2 <= age <= max_age_s):
        return None
    if signal.risk_checked_at is not None:
        risk_age = (time.time() if now is None else now) - signal.risk_checked_at
        if (signal.risk_checked_at <= 0 or signal.risk_checked_at < signal.received_at - 2
                or not math.isfinite(risk_age) or risk_age < -2
                or signal.status not in {SOCIAL_STATUS_PRESENT, SOCIAL_STATUS_SUSPICIOUS}):
            return None
    if signal.risk_flags and (signal.status != SOCIAL_STATUS_SUSPICIOUS or signal.risk_checked_at is None):
        return None
    if signal.status != SOCIAL_STATUS_UNKNOWN:
        links = _iter_links(signal)
        if (signal.social_ok is None or signal.link_count != len(links)
                or signal.social_ok != bool(links)
                or any((getattr(signal, f"{kind}_present") is not None
                        and getattr(signal, f"{kind}_present") != bool(getattr(signal, f"{kind}_url")))
                       or (getattr(signal, f"{kind}_url") is not None and getattr(signal, f"{kind}_present") is not True)
                       for kind in ("twitter", "telegram", "discord", "website"))):
            return None
    elif (signal.social_ok is not None or _iter_links(signal) or signal.link_count
          or signal.risk_flags or signal.confidence_bonus
          or any((signal.twitter_present, signal.telegram_present, signal.discord_present, signal.website_present))):
        return None
    return signal


def social_cache_ttl_s(signal: SocialSignal | None = None) -> float:
    configured = _finite(getattr(CFG, "SOCIALS_CACHE_TTL_S", SOCIAL_MAX_AGE_S), SOCIAL_MAX_AGE_S)
    ttl = min(SOCIAL_MAX_AGE_S, max(1., configured))
    return min(60., ttl) if signal is not None and signal.status == SOCIAL_STATUS_UNKNOWN else ttl


def social_profile_identity(signal: SocialSignal) -> tuple:
    """Same observed profile/coverage, not same response or cached clock."""
    return (signal.social_ok, signal.twitter_present, signal.telegram_present, signal.discord_present,
            signal.website_present, signal.link_count, signal.twitter_url, signal.telegram_url,
            signal.discord_url, signal.website_url)


def social_feature_values(signal: SocialSignal) -> dict[str, Any]:
    """No measured-absence zeros for an unknown profile in a model vector."""
    known = signal.status != SOCIAL_STATUS_UNKNOWN and signal.social_ok is not None
    return {"social_status": signal.status, "social_ok": signal.social_ok if known else None,
        **{f"{kind}_present": int(getattr(signal, f"{kind}_present"))
           if known and getattr(signal, f"{kind}_present") is not None else None
           for kind in ("twitter", "telegram", "discord", "website")},
        "social_link_count": signal.link_count if known else None,
        "social_confidence_bonus": signal.confidence_bonus if known else None,
        "social_risk_flags": ",".join(signal.risk_flags), "social_latency_ms": signal.latency_ms}


def apply_social_signal_to_token(token: dict[str, Any], signal: SocialSignal) -> dict[str, Any]:
    token["social_signal"] = signal.to_dict()
    token.update(social_feature_values(signal))
    token["social_source"] = signal.source
    token["social_address"] = signal.address
    token["social_received_at"] = signal.received_at
    token["social_receipt_version"] = signal.receipt_version
    token["social_basis"] = signal.basis
    token["social_risk_checked_at"] = signal.risk_checked_at
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
        return replace(signal, risk_checked_at=time.time())
    return replace(
        signal,
        status=SOCIAL_STATUS_SUSPICIOUS,
        risk_flags=tuple(sorted(risk_flags)),
        confidence_bonus=max(0.0, signal.confidence_bonus - float(getattr(CFG, "GREEN_SNIPER_SOCIALS_RISK_PENALTY", 5.0) or 5.0)),
        risk_checked_at=time.time(),
    )


def record_social_links(
    signal: SocialSignal,
    *,
    address: str | None,
    symbol: str | None = None,
    path: Path = SOCIAL_LINKS_SEEN_PATH,
) -> None:
    if signal.address is not None and signal.address != address:
        raise ValueError("Social receipt and link-history token identities conflict")
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
                        "received_at": signal.received_at,
                        "receipt_version": signal.receipt_version,
                        "basis": signal.basis,
                        "risk_checked_at": signal.risk_checked_at,
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
    if signal.address is not None and signal.address != address:
        raise ValueError("Social receipt and signal-history token identities conflict")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts_utc": utc_now().isoformat(),
        "symbol": symbol,
        "lane": lane,
        **signal.to_dict(),
        "address": address,
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
    "checked_social_receipt",
    "social_feature_values",
    "social_cache_ttl_s",
    "social_profile_identity",
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
