from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path

from analytics.research_runtime import (
    RESEARCH_EVENTS_PATH,
    RESEARCH_SCORECARD_JSON,
    RESEARCH_THRESHOLDS_JSON,
)
from config.config import CFG, PROJECT_ROOT
from runtime.process_manager import bot_process_console_log_path, bot_process_state_path
from utils.runtime_telemetry import RUNTIME_EVENTS_PATH


def _resolve_path(path: str | Path) -> Path:
    raw = Path(path).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return (PROJECT_ROOT / raw).resolve()


_ALLOWED_UI_ROLES = {"viewer", "operator", "admin"}


@dataclass(frozen=True)
class LocalAuthUserConfig:
    username: str
    password: str
    role: str
    display_name: str


def _normalize_auth_mode(raw: str | None) -> str:
    value = str(raw or "local").strip().lower()
    if value not in {"local", "dev"}:
        return "local"
    return value


def _normalize_ui_role(raw: str | None) -> str:
    value = str(raw or "viewer").strip().lower()
    if value not in _ALLOWED_UI_ROLES:
        return "viewer"
    return value


def _default_local_auth_users() -> tuple[LocalAuthUserConfig, ...]:
    return (
        LocalAuthUserConfig(username="viewer", password="viewer", role="viewer", display_name="Viewer"),
        LocalAuthUserConfig(username="operator", password="operator", role="operator", display_name="Operator"),
        LocalAuthUserConfig(username="admin", password="admin", role="admin", display_name="Admin"),
    )


def _parse_local_auth_users(raw: str | None) -> tuple[tuple[LocalAuthUserConfig, ...], bool]:
    if raw is None or not str(raw).strip():
        return _default_local_auth_users(), True

    users: list[LocalAuthUserConfig] = []
    seen: set[str] = set()
    for chunk in str(raw).split(";"):
        item = chunk.strip()
        if not item:
            continue
        parts = [segment.strip() for segment in item.split(":")]
        if len(parts) < 3:
            continue
        username = parts[0]
        password = parts[1] or parts[0]
        role = _normalize_ui_role(parts[2])
        display_name = parts[3] if len(parts) > 3 and parts[3] else username.title()
        normalized_username = username.lower()
        if not normalized_username or normalized_username in seen:
            continue
        seen.add(normalized_username)
        users.append(
            LocalAuthUserConfig(
                username=normalized_username,
                password=password,
                role=role,
                display_name=display_name,
            )
        )

    if not users:
        return _default_local_auth_users(), True
    return tuple(users), False


@dataclass(frozen=True)
class APISettings:
    title: str
    version: str
    project_root: Path
    data_dir: Path
    runtime_dir: Path
    metrics_dir: Path
    logs_dir: Path
    db_path: Path
    features_dir: Path
    runtime_events_path: Path
    research_events_path: Path
    research_scorecard_json: Path
    research_thresholds_json: Path
    post_partial_experiment_json: Path
    recommended_threshold_json: Path
    train_status_json: Path
    dataset_quality_json: Path
    paper_portfolio_path: Path
    bot_process_state_path: Path
    bot_process_console_log_path: Path
    auth_mode: str
    session_cookie_name: str
    session_ttl_seconds: int
    session_secret: str
    session_cookie_secure: bool
    local_auth_users: tuple[LocalAuthUserConfig, ...]
    using_default_local_auth_users: bool


@lru_cache(maxsize=1)
def get_settings() -> APISettings:
    project_root = PROJECT_ROOT.resolve()
    data_dir = (project_root / "data").resolve()
    runtime_dir = (data_dir / "runtime").resolve()
    metrics_dir = (data_dir / "metrics").resolve()
    auth_users, using_default_users = _parse_local_auth_users(os.getenv("UI_LOCAL_USERS"))
    return APISettings(
        title="MemeBot 3 API",
        version="0.1.0",
        project_root=project_root,
        data_dir=data_dir,
        runtime_dir=runtime_dir,
        metrics_dir=metrics_dir,
        logs_dir=_resolve_path(CFG.LOG_PATH),
        db_path=_resolve_path(CFG.SQLITE_DB),
        features_dir=_resolve_path(CFG.FEATURES_DIR),
        runtime_events_path=Path(RUNTIME_EVENTS_PATH).resolve(),
        research_events_path=Path(RESEARCH_EVENTS_PATH).resolve(),
        research_scorecard_json=Path(RESEARCH_SCORECARD_JSON).resolve(),
        research_thresholds_json=Path(RESEARCH_THRESHOLDS_JSON).resolve(),
        post_partial_experiment_json=(metrics_dir / "post_partial_experiment.json").resolve(),
        recommended_threshold_json=(metrics_dir / "recommended_threshold.json").resolve(),
        train_status_json=(metrics_dir / "train_status.json").resolve(),
        dataset_quality_json=(metrics_dir / "dataset_quality.json").resolve(),
        paper_portfolio_path=(data_dir / "paper_portfolio.json").resolve(),
        bot_process_state_path=bot_process_state_path(project_root),
        bot_process_console_log_path=bot_process_console_log_path(project_root),
        auth_mode=_normalize_auth_mode(os.getenv("UI_AUTH_MODE")),
        session_cookie_name=(os.getenv("UI_SESSION_COOKIE_NAME") or "memebot3_ui_session").strip() or "memebot3_ui_session",
        session_ttl_seconds=max(int(os.getenv("UI_SESSION_TTL_SECONDS") or "43200"), 300),
        session_secret=(os.getenv("UI_SESSION_SECRET") or "memebot3-local-ui-session-secret").strip()
        or "memebot3-local-ui-session-secret",
        session_cookie_secure=str(os.getenv("UI_SESSION_COOKIE_SECURE") or "").strip().lower() in {"1", "true", "yes", "on"},
        local_auth_users=auth_users,
        using_default_local_auth_users=using_default_users,
    )
