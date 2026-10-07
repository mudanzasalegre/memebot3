from __future__ import annotations

from types import SimpleNamespace

from ml.labels import moonshot_execution_label, moonshot_time_to_peak_min


def _row(**overrides):
    row = {
        "address": "Moon111111111111111111111111111111111111pump",
        "source": "pumpfun",
        "age_minutes": 2,
        "txns_last_5m": 320,
        "market_cap_usd": 80_000,
        "price_pct_5m": 650,
        "has_jupiter_route": True,
        "cluster_bad": False,
        "time_to_peak_sec": 180,
        "max_pnl_pct": 450,
    }
    row.update(overrides)
    return row


def test_moonshot_time_to_peak_prefers_explicit_seconds() -> None:
    value, source = moonshot_time_to_peak_min(_row(time_to_peak_sec=75))

    assert value == 1.25
    assert source == "seconds"


def test_executable_and_theoretical_moonshot_are_separate_labels() -> None:
    executable = moonshot_execution_label(_row(has_jupiter_route=False))
    theoretical_only = moonshot_execution_label(
        _row(
            txns_last_5m=120,
            cluster_bad=True,
            reason="moonshot_micro_lottery_shadow:cluster_bad",
        )
    )

    assert executable["theoretical_moonshot"] is True
    assert executable["executable_moonshot"] is True
    assert executable["moonshot_route_viability"] == "route_proxy_paper_only"
    assert executable["moonshot_viability"] == "executable"

    assert theoretical_only["theoretical_moonshot"] is True
    assert theoretical_only["executable_moonshot"] is False
    assert theoretical_only["moonshot_viability"] == "theoretical_only"
    assert "cluster_bad" in theoretical_only["moonshot_blocker"]
    assert theoretical_only["moonshot_time_to_peak_min"] == 3.0


def test_risky_cluster_label_is_ultra_micro_only() -> None:
    allowed_cfg = SimpleNamespace(
        MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_MODE_ENABLED=True,
        MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL=0.0005,
    )
    blocked_cfg = SimpleNamespace(
        MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_MODE_ENABLED=True,
        MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL=0.0006,
    )

    allowed = moonshot_execution_label(_row(cluster_bad=True), cfg=allowed_cfg)
    blocked = moonshot_execution_label(_row(cluster_bad=True), cfg=blocked_cfg)

    assert allowed["executable_moonshot"] is True
    assert allowed["moonshot_amount_sol"] == 0.0005
    assert allowed["moonshot_cluster_viability"] == "risky_cluster_ultra_micro_only"

    assert blocked["executable_moonshot"] is False
    assert blocked["moonshot_cluster_viability"] == "cluster_bad_blocked"
