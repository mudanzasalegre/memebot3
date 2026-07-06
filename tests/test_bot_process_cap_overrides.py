from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from api.auth import role_permissions
from api.services import bot_process
from api.services.common import make_source_status


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        project_root=tmp_path,
        bot_process_state_path=tmp_path / "data" / "runtime" / "ui_managed_bot_process.json",
        bot_process_console_log_path=tmp_path / "logs" / "ui_managed_bot.console.log",
    )


def _stopped_snapshot(*args, **kwargs):
    return (
        {
            "status": "stopped",
            "can_start": True,
            "can_stop": False,
        },
        make_source_status(source_key="test.process", kind="process", status="empty"),
    )


def test_ui_managed_paper_start_passes_paper_cap_override(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(bot_process, "get_bot_process_snapshot", _stopped_snapshot)

    def fake_start(*args, **kwargs):
        captured.update(kwargs)
        return {"pid": 123}

    monkeypatch.setattr(bot_process, "start_managed_bot_process", fake_start)

    bot_process.start_bot_process_envelope(
        _settings(tmp_path),
        requested_by="admin",
        dry_run=True,
        paper_max_invested_sol=3.0,
    )

    assert captured["dry_run"] is True
    assert captured["env_overrides"] == {"PAPER_MAX_INVESTED_SOL": "3"}


def test_ui_managed_live_start_passes_live_cap_and_profile(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    profile_path = tmp_path / "data" / "runtime" / "live_start.env"
    monkeypatch.setattr(bot_process, "get_bot_process_snapshot", _stopped_snapshot)
    monkeypatch.setattr(bot_process, "get_runtime_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        bot_process,
        "build_live_promotion_preflight",
        lambda *args, **kwargs: {"passed": True, "gates": [], "profile_path": str(profile_path)},
    )
    monkeypatch.setattr(bot_process, "write_live_start_profile", lambda *args, **kwargs: profile_path)

    def fake_start(*args, **kwargs):
        captured.update(kwargs)
        return {"pid": 123}

    monkeypatch.setattr(bot_process, "start_managed_bot_process", fake_start)

    bot_process.start_bot_process_envelope(
        _settings(tmp_path),
        requested_by="admin",
        dry_run=False,
        confirm_live=True,
        live_max_invested_sol=1.0,
    )

    assert captured["dry_run"] is False
    assert captured["env_overrides"] == {
        "CONFIG_PROFILE_PATH": str(profile_path),
        "LIVE_MAX_INVESTED_SOL": "1",
    }


def test_full_stack_stop_is_admin_only() -> None:
    assert "control.stack.stop" in role_permissions("admin")
    assert "control.stack.stop" not in role_permissions("operator")
