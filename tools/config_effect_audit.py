from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.report_utils import write_json, write_markdown
from config.config import PROJECT_ROOT


FLAG_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]+)\s*=")
IDENT_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
CODE_EXTS = {".py", ".ps1", ".md", ".env"}


def _flags_in_env_file(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = FLAG_RE.match(line)
        if match:
            out.add(match.group(1))
    return out


def _iter_source_files(root: Path) -> list[Path]:
    skip = {".venv", "__pycache__", ".pytest_cache", "data"}
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in CODE_EXTS:
            continue
        if any(part in skip for part in path.parts):
            continue
        files.append(path)
    return files


def build_config_effect_audit(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    env_files = [root / ".env.example", *sorted((root / "config" / "profiles").glob("*.env"))]
    flags: dict[str, dict[str, Any]] = {}
    for env_file in env_files:
        for flag in _flags_in_env_file(env_file):
            item = flags.setdefault(flag, {"defined_in": [], "code_references": 0, "test_references": 0, "profile_enabled": False})
            item["defined_in"].append(str(env_file.relative_to(root)))
            text = env_file.read_text(encoding="utf-8", errors="ignore")
            if re.search(rf"^\s*{re.escape(flag)}\s*=\s*(1|true|yes|on)\s*$", text, re.IGNORECASE | re.MULTILINE):
                item["profile_enabled"] = True

    code_counts: Counter[str] = Counter()
    test_counts: Counter[str] = Counter()
    for path in _iter_source_files(root):
        if path.name.endswith(".env"):
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == "docs":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        counts = Counter(IDENT_RE.findall(text))
        if rel.parts and rel.parts[0] == "tests":
            test_counts.update(counts)
        else:
            code_counts.update(counts)

    for flag, item in flags.items():
        code_refs = code_counts.get(flag, 0)
        test_refs = test_counts.get(flag, 0)
        item["code_references"] = code_refs
        item["test_references"] = test_refs
        item["status"] = (
            "active_tested"
            if code_refs and test_refs
            else "active_untested"
            if code_refs
            else "possible_placebo_enabled"
            if item["profile_enabled"]
            else "possible_placebo"
        )
    return {"flags": dict(sorted(flags.items())), "summary": _summary(flags)}


def _summary(flags: dict[str, dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in flags.values():
        key = str(item.get("status") or "unknown")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def write_config_effect_audit(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_config_effect_audit(root)
    write_json(root / "data" / "metrics" / "config_effect_audit.json", report)
    lines = ["# Config Effect Audit", "", "| Flag | Status | Code refs | Test refs | Enabled in profile |", "|---|---|---:|---:|---|"]
    for flag, item in report["flags"].items():
        lines.append(
            f"| `{flag}` | `{item['status']}` | {item['code_references']} | {item['test_references']} | {bool(item['profile_enabled'])} |"
        )
    write_markdown(root / "docs" / "CONFIG_EFFECT_AUDIT.md", lines)
    return report


def main() -> int:
    report = write_config_effect_audit()
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
