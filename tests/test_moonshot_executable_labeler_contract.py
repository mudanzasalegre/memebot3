import pandas as pd

from ml.family_training import train_classifier_family
from ml.label_builder import build_labels


def test_executable_moonshot_peak_head_requires_executable_route():
    labels = build_labels(
        pd.DataFrame(
            {
                "address": ["blocked111111111111111111111111111111pump"],
                "source": ["pumpfun"],
                "age_minutes": [2],
                "txns_last_5m": [120],
                "market_cap_usd": [80_000],
                "price_pct_5m": [650],
                "has_jupiter_route": [0],
                "liquidity_is_proxy": [1],
                "cluster_bad": [1],
                "reason": ["moonshot_micro_lottery_shadow:cluster_bad"],
                "time_to_peak_sec": [180],
                "max_pnl_pct": [500],
                "target_total_pnl_pct": [10],
            }
        )
    )

    assert labels.loc[0, "moonshot_peak100"] == 1
    assert labels.loc[0, "executable_moonshot"] == 0
    assert labels.loc[0, "executable_moonshot_peak100"] == 0
    assert labels.loc[0, "executable_moonshot_peak500"] == 0


def test_classifier_family_reports_moonshot_recall_metric(tmp_path):
    frame = pd.DataFrame(
        {
            "address": [f"token{i:02d}111111111111111111111111111111pump" for i in range(12)],
            "source": ["pumpfun"] * 12,
            "entry_lane": ["a"] * 6 + ["b"] * 6,
            "entry_regime_code": list(range(12)),
            "dex_id_code": list(range(12)),
            "price_source_quality": [1] * 12,
            "age_minutes": list(range(1, 13)),
            "queue_attempts": [0] * 12,
            "queue_age_minutes": [0] * 12,
            "snapshot_missing_fields": [0] * 12,
            "coverage_core_fields": [1] * 12,
            "liquidity_usd": [10_000] * 12,
            "volume_24h_usd": [20_000] * 12,
            "market_cap_usd": [50_000] * 12,
            "txns_last_5m": [300] * 12,
            "txns_last_5m_buys": [200] * 12,
            "txns_last_5m_sells": [100] * 12,
            "holders": [50] * 12,
            "rug_score": [0] * 12,
            "cluster_bad": [0] * 12,
            "mint_auth_renounced": [1] * 12,
            "price_pct_1m": list(range(12)),
            "price_pct_5m": [650, 620, 610, 50, 45, 40, 630, 610, 20, 15, 10, 5],
            "price5m_bucket_code": [1] * 12,
            "green_sniper_score": list(range(12)),
            "volume_pct_5m": [10] * 12,
            "price_impact_pct": [5] * 12,
            "impact_zero_flag": [0] * 12,
            "social_ok": [1] * 12,
            "social_link_count": [2] * 12,
            "social_confidence_bonus": [1] * 12,
            "twitter_followers": [100] * 12,
            "discord_members": [100] * 12,
            "score_total": [40] * 12,
            "trend": [1] * 12,
            "has_jupiter_route": [1] * 12,
            "require_jupiter_for_buy": [1] * 12,
            "route_proxy": [0] * 12,
            "liquidity_is_proxy": [0] * 12,
            "venue_is_pumpswap": [1] * 12,
            "mcap_bucket_code": [1] * 12,
            "missing_liquidity": [0] * 12,
            "missing_volume": [0] * 12,
            "missing_holders": [0] * 12,
            "missing_rug_score": [0] * 12,
            "missing_socials": [0] * 12,
            "missing_trend": [0] * 12,
            "max_pnl_pct": [160, 180, 200, 40, 35, 30, 170, 190, 20, 15, 10, 5],
            "target_total_pnl_pct": [20, 25, 30, -5, -4, -3, 22, 28, -2, -1, -1, -1],
        }
    )

    report = train_classifier_family(
        family="runner",
        targets=["executable_moonshot_peak100"],
        feature_set_name="runner_features",
        frame=frame,
        output_dir=tmp_path,
        min_rows=10,
    )

    target = report["targets"]["executable_moonshot_peak100"]
    assert target["status"] == "trained"
    assert "recall_at_k" in target
