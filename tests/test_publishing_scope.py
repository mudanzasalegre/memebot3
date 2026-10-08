from pathlib import Path

import pytest

from types import SimpleNamespace

from tools import sync_publishing_checkout as sync
from tools.sync_publishing_checkout import allowed_source


@pytest.mark.parametrize("path", [".env", "data/paper_portfolio.json", "logs/runtime.json", "work/result.json",
                                  "ml/models/runner/model.meta.json", "ml/model_registry.json",
                                  "config/profiles/paper_research_candidate_generated.env", "ui/node_modules/pkg/index.js",
                                  "ml/entry.meta.json", "secret.txt", "ml/operator.ipynb", "data/audit.ipynb"])
def test_runtime_or_sensitive_files_are_not_publication_sources(path):
    assert not allowed_source(Path(path))


@pytest.mark.parametrize("path", [".env.example", "run_bot.py", "ml/temporal_validation.py", "analytics/runner_price_policy.py",
                                  "config/profiles/paper_hotfix_0707.env", "docs/audits/architecture_goal_20261007.json",
                                  "ui/package-lock.json", "tests/test_exact_paper_trade_size.py", ".github/workflows/ci.yml",
                                  "docs/audits/jupiter_price_quality_20261008.ipynb"])
def test_reviewed_source_scopes_are_publishable(path):
    assert allowed_source(Path(path))


@pytest.mark.parametrize("line_end", ["\n", "\r\n"])
def test_blank_secret_assignment_does_not_consume_next_config_line(tmp_path, monkeypatch, line_end):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / ".env.example").write_bytes(line_end.join([
        "COINGECKO_DEMO_API_KEY=", "COINGECKO_SOL_TTL=60", "SOL_PRIVATE_KEY=your-placeholder", ""]).encode())
    monkeypatch.setattr(sync.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=""))
    assert [row["path"] for row in sync.build_plan(source, destination)] == [".env.example"]


def test_blank_secret_line_never_hides_real_assignment_on_next_line(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / ".env.example").write_text("COINGECKO_DEMO_API_KEY=\nSOL_PRIVATE_KEY=synthetic-nonplaceholder\n", encoding="utf-8")
    monkeypatch.setattr(sync.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=""))
    with pytest.raises(ValueError, match="nonplaceholder_secret_assignment:.env.example:SOL_PRIVATE_KEY"):
        sync.build_plan(source, destination)
