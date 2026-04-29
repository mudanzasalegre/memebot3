from __future__ import annotations

from runtime.provider_health import provider_health_snapshot


def test_provider_health_reports_gecko_degraded(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "bot.txt").write_text("429 gecko\n" * 600, encoding="utf-8")
    report = provider_health_snapshot(tmp_path)
    assert report["providers"]["gecko"]["status"] in {"degraded", "critical"}
