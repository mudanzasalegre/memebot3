from pathlib import Path

import pytest

from tools.sync_publishing_checkout import allowed_source


@pytest.mark.parametrize("path", [".env", "data/paper_portfolio.json", "logs/runtime.json", "work/result.json",
                                  "ml/models/runner/model.meta.json", "ml/model_registry.json",
                                  "config/profiles/paper_research_candidate_generated.env", "ui/node_modules/pkg/index.js",
                                  "ml/entry.meta.json", "secret.txt"])
def test_runtime_or_sensitive_files_are_not_publication_sources(path):
    assert not allowed_source(Path(path))


@pytest.mark.parametrize("path", [".env.example", "run_bot.py", "ml/temporal_validation.py", "analytics/runner_price_policy.py",
                                  "config/profiles/paper_hotfix_0707.env", "docs/audits/architecture_goal_20261007.json",
                                  "ui/package-lock.json", "tests/test_exact_paper_trade_size.py", ".github/workflows/ci.yml"])
def test_reviewed_source_scopes_are_publishable(path):
    assert allowed_source(Path(path))
