from __future__ import annotations

import json
from types import SimpleNamespace

from analytics.social_signal import (
    SOCIAL_STATUS_MISSING,
    SOCIAL_STATUS_PRESENT,
    SOCIAL_STATUS_SUSPICIOUS,
    flag_suspicious_links,
    social_signal_from_profile,
    social_signal_from_token,
)


def test_social_profile_present_links() -> None:
    signal = social_signal_from_profile(
        {
            "links": {
                "twitterUrl": "https://x.com/token",
                "telegramUrl": "t.me/token",
                "website": "token.example",
            }
        },
        latency_ms=123,
    )

    assert signal.status == SOCIAL_STATUS_PRESENT
    assert signal.social_ok is True
    assert signal.twitter_present is True
    assert signal.telegram_present is True
    assert signal.website_present is True
    assert signal.link_count == 3
    assert signal.latency_ms == 123


def test_social_profile_missing_links() -> None:
    signal = social_signal_from_profile({"links": {}})

    assert signal.status == SOCIAL_STATUS_MISSING
    assert signal.social_ok is False
    assert signal.link_count == 0


def test_social_token_false_is_missing() -> None:
    signal = social_signal_from_token({"social_ok": False})

    assert signal.status == SOCIAL_STATUS_MISSING
    assert signal.social_ok is False


def test_reused_link_sets_suspicious_flag(tmp_path, monkeypatch) -> None:
    import analytics.social_signal as social_signal

    monkeypatch.setattr(
        social_signal,
        "CFG",
        SimpleNamespace(
            SOCIALS_SUSPICIOUS_ENABLED=True,
            SOCIALS_REUSED_LINK_MAX_TOKENS=2,
            GREEN_SNIPER_SOCIALS_RISK_PENALTY=5,
        ),
    )
    history = tmp_path / "links.jsonl"
    history.write_text(
        "\n".join(
            json.dumps({"address": address, "url": "https://t.me/reused", "domain": "t.me"})
            for address in ("a", "b")
        ),
        encoding="utf-8",
    )
    signal = social_signal_from_profile({"links": {"telegramUrl": "https://t.me/reused"}})

    suspicious = flag_suspicious_links(signal, address="c", history_path=history)

    assert suspicious.status == SOCIAL_STATUS_SUSPICIOUS
    assert "reused_link" in suspicious.risk_flags
