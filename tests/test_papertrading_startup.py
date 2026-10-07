from __future__ import annotations

import pytest
import os
from pathlib import Path
import subprocess
import sys

from trader import papertrading


def test_paper_and_exit_imports_never_initialize_live_signer(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if key not in {"SOL_PRIVATE_KEY", "SOL_PUBLIC_KEY"}}
    env["PYTHONPATH"] = str(root)
    env["DRY_RUN"] = "1"
    code = "from trader import papertrading,buyer,seller; import sys; assert 'trader.sol_signer' not in sys.modules; print('paper_import_no_signer')"
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("paper_import_no_signer")


@pytest.mark.asyncio
async def test_empty_portfolio_backfill_does_not_fetch_sol_price(monkeypatch) -> None:
    monkeypatch.setattr(papertrading, "_PORTFOLIO", {})

    async def unexpected_price_fetch() -> float:
        raise AssertionError("empty portfolio must not touch the SOL price provider")

    monkeypatch.setattr(papertrading, "get_sol_usd", unexpected_price_fetch)

    assert await papertrading.backfill_entry_notionals() == 0
