from __future__ import annotations

from runtime.provider_health import provider_health_snapshot


def test_provider_health_reports_gecko_degraded(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "bot.txt").write_text("gecko HTTP 429 Too Many Requests\n" * 600, encoding="utf-8")
    report = provider_health_snapshot(tmp_path)
    assert report["providers"]["gecko"]["status"] in {"degraded", "critical"}


def test_provider_health_ignores_provider_names_and_numeric_collisions(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "bot.txt").write_text(
        "\n".join(
            [
                "jupiter quote ok mint=Token429 rank_score=42.9",
                "gecko candidate timestamp=2026-07-10T22:29:42.900Z",
                "birdeye token=/Address404 response ok",
                "pumpportal websocket connected",
                "rugcheck result safe",
                "dexscreener pair loaded",
                "config max_missing=4 null_policy=keep",
            ]
        ),
        encoding="utf-8",
    )

    report = provider_health_snapshot(tmp_path)

    for provider in report["providers"].values():
        assert provider["status"] == "ok"
        assert provider["recent_error_signals"] == 0


def test_provider_health_counts_one_signal_per_error_line(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "bot.txt").write_text(
        "Jupiter HTTP 429 error: rate limited; request failed\n"
        "Birdeye HTTP 404 token unavailable\n"
        "PumpFun WS desconectado por timeout\n"
        "Campos críticos nulos liquidity_usd,volume_24h_usd\n",
        encoding="utf-8",
    )

    report = provider_health_snapshot(tmp_path)

    assert report["providers"]["jupiter"]["recent_error_signals"] == 1
    assert report["providers"]["birdeye"]["recent_error_signals"] == 1
    assert report["providers"]["pumpportal"]["recent_error_signals"] == 1
    assert report["providers"]["data_completeness"]["recent_error_signals"] == 1
