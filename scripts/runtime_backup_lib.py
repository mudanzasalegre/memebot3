from __future__ import annotations

import datetime as dt
import json
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
METRICS_DIR = DATA_DIR / "metrics"
FEATURES_DIR = DATA_DIR / "features"
BACKUPS_DIR = PROJECT_ROOT / "backups"
UTC = dt.timezone.utc
RESTORE_SKIP_NAMES = {
    "data/memebotdatabase.db-wal",
    "data/memebotdatabase.db-shm",
}

BASE_ALLOWLIST = {
    Path("data/memebotdatabase.db"),
    Path("data/memebotdatabase.db-wal"),
    Path("data/memebotdatabase.db-shm"),
    Path("data/paper_portfolio.json"),
    Path("data/research_portfolio.json"),
    Path("data/metrics/runtime_events.jsonl"),
    Path("data/metrics/candidate_outcomes.jsonl"),
    Path("data/metrics/research_scorecard.json"),
    Path("data/metrics/research_scorecard.md"),
    Path("data/metrics/research_thresholds.json"),
    Path("data/metrics/recommended_threshold.json"),
    Path("data/metrics/train_status.json"),
    Path("data/metrics/dataset_quality.json"),
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def backup_timestamp() -> str:
    return utc_now().strftime("%Y%m%d-%H%M%S")


def default_archive_path() -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUPS_DIR / f"memebot3-backup-{backup_timestamp()}.zip"


def _feature_files() -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.parquet", "*.csv"):
        files.extend(sorted(FEATURES_DIR.glob(pattern)))
    return [path.relative_to(PROJECT_ROOT) for path in files if path.is_file()]


def _latest_log_files(limit: int = 3) -> list[Path]:
    logs_dir = PROJECT_ROOT / "logs"
    if not logs_dir.exists():
        return []
    candidates = sorted(
        [path for path in logs_dir.glob("*.txt") if path.is_file()],
        key=lambda item: item.stat().st_mtime,
    )
    return [path.relative_to(PROJECT_ROOT) for path in candidates[-limit:]]


def collect_backup_paths(*, include_env: bool, include_logs: bool) -> list[Path]:
    paths = list(BASE_ALLOWLIST)
    paths.extend(_feature_files())
    if include_env:
        paths.append(Path(".env"))
    if include_logs:
        paths.extend(_latest_log_files())
    existing = []
    seen: set[str] = set()
    for relative in sorted(paths, key=lambda item: item.as_posix()):
        normalized = relative.as_posix()
        if normalized in seen:
            continue
        seen.add(normalized)
        absolute = PROJECT_ROOT / relative
        if absolute.exists() and absolute.is_file():
            existing.append(relative)
    return existing


def create_backup_archive(
    archive_path: Path,
    *,
    include_env: bool,
    include_logs: bool,
    note: str | None = None,
) -> Path:
    archive_path = archive_path.resolve()
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    files = collect_backup_paths(include_env=include_env, include_logs=include_logs)
    manifest = {
        "created_at_utc": utc_now().isoformat(),
        "repo_root": str(PROJECT_ROOT),
        "include_env": bool(include_env),
        "include_logs": bool(include_logs),
        "note": note,
        "files": [path.as_posix() for path in files],
    }

    with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("backup_manifest.json", json.dumps(manifest, ensure_ascii=True, indent=2))
        for relative in files:
            archive.write(PROJECT_ROOT / relative, arcname=relative.as_posix())
    return archive_path


def _is_safe_member(name: str, *, include_env: bool) -> bool:
    if name == "backup_manifest.json":
        return True
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts:
        return False
    if name == ".env":
        return include_env
    return (
        name.startswith("data/")
        or name.startswith("logs/")
    )


def iter_archive_members(archive_path: Path, *, include_env: bool) -> Iterable[str]:
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        for member in archive.namelist():
            if _is_safe_member(member, include_env=include_env):
                yield member


def restore_backup_archive(
    archive_path: Path,
    *,
    include_env: bool,
) -> list[Path]:
    archive_path = archive_path.resolve()
    restored: list[Path] = []
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        for member in iter_archive_members(archive_path, include_env=include_env):
            if member == "backup_manifest.json":
                continue
            if member in RESTORE_SKIP_NAMES:
                continue
            target = (PROJECT_ROOT / PurePosixPath(member)).resolve()
            if PROJECT_ROOT not in target.parents and target != PROJECT_ROOT:
                raise RuntimeError(f"unsafe restore target: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, mode="r") as source, target.open("wb") as destination:
                destination.write(source.read())
            restored.append(target)
    return restored
