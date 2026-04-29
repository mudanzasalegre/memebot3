from __future__ import annotations

import argparse
import sys
from pathlib import Path

from runtime_backup_lib import create_backup_archive, default_archive_path, restore_backup_archive


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


def _require_project_python() -> None:
    print(f"python_executable={sys.executable}")
    if EXPECTED_PYTHON.exists() and Path(sys.executable).resolve() != EXPECTED_PYTHON.resolve():
        raise SystemExit(
            "Runtime restore must be executed with the project venv. "
            f"Use: {EXPECTED_PYTHON} scripts/runtime_restore.py <archive.zip> --force"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a basic runtime backup bundle.")
    parser.add_argument("archive", type=Path, help="Backup zip archive to restore")
    parser.add_argument("--force", action="store_true", help="Required to overwrite current runtime state")
    parser.add_argument("--with-env", action="store_true", help="Restore .env if it exists in the archive")
    parser.add_argument("--skip-pre-backup", action="store_true", help="Skip the safety pre-restore backup")
    args = parser.parse_args()

    _require_project_python()

    archive_path = args.archive.resolve()
    if not archive_path.exists():
        raise SystemExit(f"archive not found: {archive_path}")
    if not args.force:
        raise SystemExit("restore requires --force")

    if not args.skip_pre_backup:
        pre_backup = create_backup_archive(
            default_archive_path(),
            include_env=bool(args.with_env),
            include_logs=False,
            note=f"pre-restore snapshot before {archive_path.name}",
        )
        print(f"pre_restore_backup={pre_backup}")

    restored = restore_backup_archive(
        archive_path,
        include_env=bool(args.with_env),
    )
    print(f"runtime_restore.count={len(restored)}")
    for path in restored:
        print(path)


if __name__ == "__main__":
    main()
