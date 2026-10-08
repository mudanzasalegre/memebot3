"""Copy reviewed source to a clean publishing checkout, never mirror/delete data."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

SOURCE_DIRS = {".github", "analytics", "api", "assets", "backtest", "config", "db", "docs", "execution",
               "features", "fetcher", "labeler", "ml", "research_loop", "runtime", "scripts", "tests", "tools", "trader", "ui", "utils"}
ROOT_FILES = {".gitignore", ".env.example", "README.md", "Makefile", "requirements.txt", "run_bot.py", "trade_pnl.py"}
SUFFIXES = {".py", ".ps1", ".sh", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
            ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".html", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".txt"}
EXCLUDED_PARTS = {"node_modules", "dist", ".vite", "__pycache__", ".pytest_cache", ".ruff_cache", "backups", "versions"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".ico"}
SECRET_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Z0-9_]*(?:PRIVATE_KEY|API_KEY|PASSWORD|SECRET)[A-Z0-9_]*)\s*=\s*(.*)$", re.M)


def allowed_source(relative: Path) -> bool:
    parts = relative.parts
    if not parts or any(part in EXCLUDED_PARTS for part in parts):
        return False
    if len(parts) == 1:
        return relative.name in ROOT_FILES
    if parts[0] not in SOURCE_DIRS or parts[:2] == ("ml", "models"):
        return False
    if relative.name in {"model_registry.json", "advisory_manifest.json"}:
        return False
    if parts[0] == "ml" and relative.name.endswith(".meta.json"):
        return False
    if relative.suffix == ".env":
        return parts[:2] == ("config", "profiles") and not relative.name.startswith("paper_research_candidate_")
    if relative.suffix == ".ipynb":
        return parts[:2] == ("docs", "audits")
    return relative.suffix in SUFFIXES or relative.name == ".gitkeep"


def _content(path: Path) -> bytes:
    raw = path.read_bytes()
    return raw if path.suffix in BINARY_SUFFIXES else raw.replace(b"\r\n", b"\n")


def build_plan(source: Path, destination: Path) -> list[dict]:
    source, destination = source.resolve(), destination.resolve()
    if source == destination:
        raise ValueError("source_and_destination_must_differ")
    subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=destination, check=True, capture_output=True)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=destination, check=True, capture_output=True, text=True)
    if dirty.stdout.strip():
        raise ValueError("publishing_checkout_has_changes_preserve_them")
    plan = []
    for directory, children, names in os.walk(source):
        relative_dir = Path(directory).relative_to(source)
        children[:] = [name for name in children if name not in EXCLUDED_PARTS
                       and (relative_dir.parts or name in SOURCE_DIRS)
                       and (relative_dir.parts + (name,)) != ("ml", "models")]
        for name in names:
            relative = relative_dir / name
            if not allowed_source(relative):
                continue
            incoming, target = source / relative, destination / relative
            if not incoming.resolve().is_relative_to(source) or not target.resolve().is_relative_to(destination):
                raise ValueError(f"source_or_destination_escapes_root:{relative.as_posix()}")
            content = _content(incoming)
            if target.exists() and _content(target) == content:
                continue
            if incoming.suffix == ".env" or incoming.name == ".env.example":
                for match in SECRET_ASSIGNMENT.finditer(content.decode("utf-8")):
                    value = match.group(2).strip().strip("\"'")
                    is_flag = "REQUIRE_API_KEY" in match.group(1) and value.lower() in {"true", "false", "0", "1"}
                    if value and not is_flag and not value.startswith(("#", "YOUR_", "your_", "YOUR-", "your-", "<", "REPLACE_", "REDACTED_")):
                        raise ValueError(f"nonplaceholder_secret_assignment:{relative.as_posix()}:{match.group(1)}")
            plan.append({"path": relative.as_posix(), "action": "update" if target.exists() else "add",
                         "source_sha256": hashlib.sha256(incoming.read_bytes()).hexdigest()})
    return sorted(plan, key=lambda row: row["path"])


def apply_plan(source: Path, destination: Path, plan: list[dict]) -> None:
    for item in plan:
        incoming, target = source / item["path"], destination / item["path"]
        if hashlib.sha256(incoming.read_bytes()).hexdigest() != item["source_sha256"]:
            raise ValueError(f"source_changed_after_plan:{item['path']}")
        if not target.resolve().is_relative_to(destination.resolve()):
            raise ValueError("destination_escapes_root")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(incoming, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Copy the plan; default is read-only")
    parser.add_argument("--show-plan", action="store_true")
    args = parser.parse_args()
    plan = build_plan(args.source, args.destination)
    if args.apply:
        apply_plan(args.source.resolve(), args.destination.resolve(), plan)
    summary = {"mode": "applied" if args.apply else "dry_run", "files": len(plan), "deleted": 0,
               "actions": dict(Counter(item["action"] for item in plan)),
               "by_scope": dict(Counter(item["path"].split("/")[0] for item in plan)),
               "plan_sha256": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()}
    if args.show_plan:
        summary["plan"] = plan
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
