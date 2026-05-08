from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
PROFILES = ROOT / "config" / "profiles"
SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PRIVATE", "RPC_URL", "PASSWORD", "AUTH")


def _clean_value(value: str) -> str:
    out = value.strip()
    if out and out[0] not in {"'", '"'} and " #" in out:
        out = out.split(" #", 1)[0].rstrip()
    return out


def _parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        out[key.strip()] = _clean_value(value)
    return out


def _render(values: dict[str, str], original: str) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for line in original.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            lines.append(line)
            continue
        key, _value = line.split("=", 1)
        key = key.strip()
        if key in values:
            lines.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            lines.append(line)
    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def _redact(key: str, value: str | None) -> str:
    if value is None:
        return "<missing>"
    if any(hint in key.upper() for hint in SECRET_HINTS):
        return "<preserved>"
    return str(value)


def _profile_values(profile_path: Path, current_values: dict[str, str]) -> dict[str, str]:
    values = _parse_env(profile_path)
    for key in list(values):
        if any(hint in key.upper() for hint in SECRET_HINTS) and key in current_values:
            values[key] = current_values[key]
    return values


def apply_profile(
    profile: str,
    *,
    dry_run: bool = False,
    env_path: Path = ENV_PATH,
    profiles_dir: Path = PROFILES,
) -> dict[str, Any]:
    profile_path = profiles_dir / f"{profile}.env"
    if not profile_path.exists():
        available = ", ".join(sorted(p.stem for p in profiles_dir.glob("*.env"))) or "(none)"
        raise SystemExit(f"profile_not_found={profile} available={available}")

    current = env_path.read_text(encoding="utf-8", errors="ignore") if env_path.exists() else ""
    current_values = _parse_env(env_path)
    values = _profile_values(profile_path, current_values)
    rendered = _render(values, current)
    rendered_values = _parse_env_from_text(rendered)

    changed: list[tuple[str, str | None, str]] = []
    added: list[tuple[str, str]] = []
    for key, new_value in values.items():
        if key not in current_values:
            added.append((key, new_value))
        elif current_values[key] != new_value:
            changed.append((key, current_values[key], new_value))

    backup: Path | None = None
    if not dry_run:
        backup = env_path.with_name(f".env.backup.{dt.datetime.now():%Y%m%d%H%M%S}")
        if env_path.exists():
            backup.write_text(current, encoding="utf-8")
        env_path.write_text(rendered, encoding="utf-8")

    return {
        "profile": profile,
        "profile_path": str(profile_path),
        "env_path": str(env_path),
        "dry_run": bool(dry_run),
        "backup": str(backup) if backup is not None else None,
        "changed": changed,
        "added": added,
        "total_keys": len(values),
        "rendered_keys": len(rendered_values),
    }


def _parse_env_from_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        out[key.strip()] = _clean_value(value)
    return out


def _print_result(result: dict[str, Any]) -> None:
    print(f"applied_profile={result['profile']}")
    print(f"dry_run={str(result['dry_run']).lower()}")
    print(f"profile_path={result['profile_path']}")
    print(f"env_path={result['env_path']}")
    print(f"backup={result['backup'] or '(not_created)'}")
    print(f"changed={len(result['changed'])}")
    print(f"added={len(result['added'])}")
    for key, old, new in result["changed"][:40]:
        print(f"change {key}: {_redact(key, old)} -> {_redact(key, new)}")
    for key, value in result["added"][:40]:
        print(f"add {key}: {_redact(key, value)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply a config profile to .env with backup.")
    parser.add_argument("profile_positional", nargs="?", help="Profile name, kept for backwards compatibility.")
    parser.add_argument("--profile", dest="profile", help="Profile name from config/profiles without .env.")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without modifying .env or creating a backup.")
    args = parser.parse_args()

    profile = str(args.profile or args.profile_positional or "").strip()
    if not profile:
        raise SystemExit("missing profile; use --profile paper_rank_research_v1")
    _print_result(apply_profile(profile, dry_run=bool(args.dry_run)))


if __name__ == "__main__":
    main()
