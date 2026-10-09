"""Birth age is not queue residence or a fabricated zero-age observation."""
import ast
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import numpy as np
import pytest

from analytics.token_time import compute_age_minutes
from analytics.green_sniper_score import score_green_sniper

NOW = dt.datetime(2026, 10, 9, 2, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("token", [{}, {"age_minutes": None}, {"queue_age_minutes": 3},
    {"created_at": True}, {"created_at": float("inf")}, {"created_at": float("nan")},
    {"age_minutes": True}, {"age_minutes": float("inf")}, {"age_minutes": -1},
    {"created_at": NOW + dt.timedelta(seconds=1)}])
def test_unknown_or_invalid_birth_age_remains_unknown(token):
    assert compute_age_minutes(token, now=NOW) is None


def test_measured_zero_age_is_not_missing():
    assert compute_age_minutes({"age_minutes": 0}, now=NOW) == 0


@pytest.mark.parametrize("value", [np.bool_(True), np.bool_(False), np.array([1]), np.array(1), [1], {"age": 1}, complex(1, 0)])
@pytest.mark.parametrize("field", ["age_minutes", "created_at"])
def test_non_numeric_scalars_or_containers_do_not_become_birth_age(field, value):
    assert compute_age_minutes({field: value}, now=NOW) is None


@pytest.mark.parametrize("value", [np.int64(0), np.float64(0)])
def test_numpy_numeric_scalar_zero_is_a_measured_age(value):
    assert compute_age_minutes({"age_minutes": value}, now=NOW) == 0


@pytest.mark.parametrize("scalar", [np.int64, np.float64])
def test_numpy_numeric_scalar_birth_clock_is_supported(scalar):
    birth = scalar((NOW - dt.timedelta(minutes=3)).timestamp())
    assert compute_age_minutes({"created_at": birth}, now=NOW) == 3


def test_queue_residence_does_not_rejuvenate_a_known_old_birth():
    assert compute_age_minutes({"created_at": NOW - dt.timedelta(minutes=25), "queue_age_minutes": 1}, now=NOW) == 25


@pytest.mark.parametrize("factor", [1, 1000, 1000000, 1000000000])
def test_shared_age_clock_supports_original_epoch_units(factor):
    birth = NOW - dt.timedelta(minutes=3)
    assert compute_age_minutes({"created_at": int(birth.timestamp()) * factor}, now=NOW) == 3


def test_unknown_age_does_not_receive_a_newborn_score_bonus():
    score = score_green_sniper({}, has_route=False, proxy_liquidity=False, live=False)
    assert score.age_component == 0


@pytest.mark.parametrize("token", [{}, {"queue_age_minutes": 4}, {"created_at": True}, {"age_minutes": True}])
def test_actual_feature_and_numeric_matrix_keep_unknown_age_missing(token):
    from features.builder import build_feature_vector
    from features.numeric_encoding import PREFIX
    from ml.feature_matrix import coerce_feature_frame
    vector = build_feature_vector({"address": "synthetic", **token}, now=NOW)
    assert pd.isna(vector["age_minutes"])
    frame = coerce_feature_frame(vector.to_frame().T, ["age_minutes", PREFIX + "age_minutes"])
    assert frame.iloc[0][PREFIX + "age_minutes"] == 1


def test_feature_matrix_distinguishes_measured_zero_birth_age():
    from features.builder import build_feature_vector
    from features.numeric_encoding import PREFIX
    from ml.feature_matrix import coerce_feature_frame
    vector = build_feature_vector({"address": "synthetic", "age_minutes": 0}, now=NOW)
    frame = coerce_feature_frame(vector.to_frame().T, ["age_minutes", PREFIX + "age_minutes"])
    assert vector["age_minutes"] == 0 and frame.iloc[0][PREFIX + "age_minutes"] == 0


@pytest.mark.parametrize("live", [False, True])
def test_green_gate_waits_for_unknown_birth_without_faking_a_birth_probe(live):
    from analytics.green_sniper_gate import evaluate_green_sniper
    decision = evaluate_green_sniper({"address": "synthetic", "discovered_via": "pumpfun"}, dry_run=not live, live=live)
    assert decision.action == "delay" and decision.reason == "missing_age"
    assert not decision.paper_birth_probe and decision.size_hint != "hot"


@pytest.mark.asyncio
async def test_actual_missing_age_delay_cannot_fall_through_to_paper_bootstrap():
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    evaluate = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_evaluate_and_buy")
    branch = next(node for node in ast.walk(evaluate) if isinstance(node, ast.If)
        and ast.unparse(node.test) == "green_decision.action == 'delay'")
    owner = ast.AsyncFunctionDef(name="probe", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[branch], decorator_list=[])
    calls = []
    async def forbidden(*args, **kwargs):
        raise AssertionError("Unknown birth cannot activate a paper bootstrap")
    namespace = {"green_decision": SimpleNamespace(action="delay", reason="missing_age"), "token": {}, "ses": None, "addr": "synthetic",
        "_defer_entry_observation": lambda token, **kwargs: calls.append(kwargs), "_maybe_apply_paper_bootstrap": forbidden}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[owner], type_ignores=[])), "run_bot.py", "exec"), namespace)
    await namespace["probe"]()
    assert calls == [{"reason": "missing_age", "stage": "green_sniper"}]
