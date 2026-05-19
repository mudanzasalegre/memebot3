from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from analytics import filters
from analytics.sniper_research_subprofiles import evaluate_sniper_research_subprofile


def test_filters_detect_toxic_initial_sell_pressure() -> None:
    assert filters.has_toxic_initial_sell_pressure(
        {
            "address": "x",
            "age_min": 2,
            "txns_last_5m": 100,
            "txns_last_5m_sells": 75,
            "price_pct_5m": 2,
        }
    )


def test_sniper_momentum_blocks_toxic_initial_sell_pressure() -> None:
    decision = evaluate_sniper_research_subprofile(
        {
            "entry_lane": "pump_early_sniper_research",
            "price_pct_5m": 140,
            "liquidity_usd": 16_000,
            "txns_last_5m": 600,
            "market_cap_usd": 55_000,
            "has_jupiter_route": True,
            "trend": "up",
            "toxic_initial_sell_pressure": 1,
        }
    )

    assert decision.allowed is False
    assert decision.reason.startswith("momentum_ignition_toxic_filter:")
    assert "momentum:toxic_initial_sell_pressure" in decision.failures


def test_run_bot_toxic_memory_expires_by_ttl() -> None:
    source = Path("run_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="run_bot.py")
    wanted = {"_prune_expiring", "_remember_toxic_initial_sell_pressure", "_toxic_initial_sell_pressure_active"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    module = ast.Module(body=body, type_ignores=[])
    namespace = {
        "time": SimpleNamespace(monotonic=lambda: 100.0),
        "CFG": SimpleNamespace(TOXIC_INITIAL_SELL_PRESSURE_TTL_S=60),
        "_toxic_initial_sell_pressure_until": {},
    }
    exec(compile(module, "run_bot.py", "exec"), namespace)

    token: dict[str, object] = {}
    namespace["_remember_toxic_initial_sell_pressure"]("A", token)
    assert namespace["_toxic_initial_sell_pressure_active"]("A", token) is True
    assert token["toxic_initial_sell_pressure"] == 1

    namespace["time"] = SimpleNamespace(monotonic=lambda: 161.0)
    assert namespace["_toxic_initial_sell_pressure_active"]("A", {}) is False
