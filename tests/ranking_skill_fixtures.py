"""Current ranking-generation declarations from isolated synthetic evidence."""
from ml.model_validation_warnings import ranking_token_skill


def current_ranking_skill():
    truth = [0, 1] * 30
    return ranking_token_skill(truth, truth, [f"ranking_fixture_mint{i}" for i in range(60)])
