from __future__ import annotations

from pathlib import Path


def project_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    return Path(__file__).resolve().parents[1]


def metrics_dir(root: str | Path | None = None) -> Path:
    return project_root(root) / "data" / "metrics"


def research_runs_dir(root: str | Path | None = None) -> Path:
    return project_root(root) / "data" / "research_runs"


__all__ = ["metrics_dir", "project_root", "research_runs_dir"]
