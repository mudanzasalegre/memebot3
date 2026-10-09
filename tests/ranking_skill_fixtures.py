"""Current ranking-generation declarations from isolated synthetic evidence."""
from ml.model_validation_warnings import ranking_token_skill
import pandas as pd


def current_ranking_skill():
    truth = [0, 1] * 30
    return ranking_token_skill(truth, truth, [f"ranking_fixture_mint{i}" for i in range(60)],
                              decision_times=pd.date_range("2026-09-01", periods=60, freq="2h", tz="UTC"))
