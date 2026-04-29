from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
PROFILES = ROOT / "config" / "profiles"
SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PRIVATE", "RPC_URL", "PASSWORD", "AUTH")


def _parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _render(values: dict[str, str], original: str) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for line in original.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            lines.append(line)
            continue
        key, _ = line.split("=", 1)
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=[p.stem for p in PROFILES.glob("*.env")])
    args = parser.parse_args()
    profile_path = PROFILES / f"{args.profile}.env"
    profile_values = _parse_env(profile_path)
    current = ENV_PATH.read_text(encoding="utf-8", errors="ignore") if ENV_PATH.exists() else ""
    current_values = _parse_env(ENV_PATH)
    for key in list(profile_values):
        if any(hint in key.upper() for hint in SECRET_HINTS) and key in current_values:
            profile_values[key] = current_values[key]
    backup = ENV_PATH.with_name(f".env.backup.{dt.datetime.now():%Y%m%d%H%M%S}")
    if ENV_PATH.exists():
        backup.write_text(current, encoding="utf-8")
    ENV_PATH.write_text(_render(profile_values, current), encoding="utf-8")
    print(f"applied_profile={args.profile}")
    print(f"backup={backup}")


if __name__ == "__main__":
    main()
