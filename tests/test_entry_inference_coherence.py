"""Isolated entry coherence; synthetic forecasts are not profitable trading."""
from __future__ import annotations

import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from analytics.inference_scope import (MAX_PREDICTIONS, inference_scope,
    scoped_prediction, scoped_snapshot, scoped_value)


def test_pin_unknown_and_detach_schema_metadata():
    calls = []
    model = object()
    def load():
        calls.append(1)
        return model, ["x"], {"nested": {"threshold": .4}}
    with inference_scope() as state:
        first = scoped_snapshot("head", load)
        first[1].append("y")
        first[2]["nested"]["threshold"] = .99
        second = scoped_snapshot("head", load)
        assert second == (model, ["x"], {"nested": {"threshold": .4}})
        assert scoped_value("absent", lambda: None) is None
        assert scoped_value("absent", lambda: object()) is None
    assert calls == [1] and state.closed and not state.values
    assert scoped_value("absent", lambda: "new") == "new"


def test_nested_entries_restore_parent_versions():
    with inference_scope():
        assert scoped_value("version", lambda: 1) == 1
        with inference_scope():
            assert scoped_value("version", lambda: 2) == 2
        assert scoped_value("version", lambda: 3) == 1
    with inference_scope():
        assert scoped_value("version", lambda: 4) == 4


def test_concurrent_async_entries_do_not_share_snapshots():
    async def entry(value):
        with inference_scope():
            before = scoped_value("head", lambda: value)
            await asyncio.sleep(0)
            return before, scoped_value("head", lambda: -1)
    async def run():
        return await asyncio.gather(*(entry(n) for n in range(12)))
    assert asyncio.run(run()) == [(n, n) for n in range(12)]


def test_cancelled_entry_clears_scope_and_late_child_does_not_pin_it():
    async def run():
        gate = asyncio.Event()
        async def child():
            await gate.wait()
            return scoped_value("head", lambda: "after")
        with pytest.raises(asyncio.CancelledError):
            with inference_scope() as state:
                scoped_value("head", lambda: "before")
                task = asyncio.create_task(child())
                raise asyncio.CancelledError()
        assert state.closed and not state.values and not state.predictions
        gate.set()
        assert await task == "after"
        assert not state.values
    asyncio.run(run())


def test_threaded_scopes_are_separate():
    def run(n):
        with inference_scope():
            return scoped_value("head", lambda: n), scoped_value("head", lambda: -1)
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(run, range(10))) == [(n, n) for n in range(10)]


def test_prediction_cache_uses_ordered_actual_float32_matrix_and_head():
    calls = []
    def predict():
        calls.append(1)
        return len(calls)
    with inference_scope() as state:
        one = pd.DataFrame([[1., 2.]], columns=["x", "y"])
        assert scoped_prediction("one", one, predict) == 1
        assert scoped_prediction("one", one.astype("float64"), predict) == 1
        changed = pd.DataFrame([[1., 3.]], columns=["x", "y"])
        assert scoped_prediction("one", changed, predict) == 2
        assert scoped_prediction("one", one[["y", "x"]], predict) == 3
        assert scoped_prediction("two", one, predict) == 4
        assert scoped_prediction("unknown", one, lambda: None) is None
        assert scoped_prediction("unknown", one, lambda: 9) is None
        assert len(state.predictions) == 5
    assert not state.predictions


def test_prediction_cache_is_bounded_and_exceptions_are_not_cached():
    with inference_scope() as state:
        for n in range(MAX_PREDICTIONS + 20):
            scoped_prediction("head", pd.DataFrame({"x": [n]}), lambda: n)
        assert len(state.predictions) == MAX_PREDICTIONS
        with pytest.raises(ValueError):
            scoped_prediction("failed", pd.DataFrame({"x": [0]}), lambda: (_ for _ in ()).throw(ValueError()))
        assert scoped_prediction("failed", pd.DataFrame({"x": [0]}), lambda: .7) == .7


def test_actual_primary_model_acceptance_and_thresholds_stay_together(tmp_path, monkeypatch):
    from test_financial_model_acceptance import setup_entry
    runtime, registry, artifact = setup_entry(tmp_path, monkeypatch)
    metadata = json.loads(artifact.meta_path.read_text())
    metadata["ai_threshold_recommended"] = .4
    artifact.meta_path.write_text(json.dumps(metadata))
    with inference_scope():
        assert runtime.should_buy({"price_pct_5m": 1}) == .5
        metadata["financial_training"]["return_basis"] = "gross"
        artifact.meta_path.write_text(json.dumps(metadata))
        runtime.reload_model()
        state = runtime.entry_prediction_state()
        assert state["activation_ready"] and state["metadata"]["ai_threshold_recommended"] == .4
        state["metadata"]["ai_threshold_recommended"] = 1
        assert runtime.entry_prediction_state()["metadata"]["ai_threshold_recommended"] == .4
        assert runtime.should_buy({"price_pct_5m": 2}) == .5
    assert runtime.should_buy({}) is None
    assert runtime.entry_prediction_state() == {"activation_ready": False, "metadata": {}}


def test_unavailable_primary_is_pinned_unknown_only_for_current_entry(tmp_path, monkeypatch):
    from test_financial_model_acceptance import setup_entry
    runtime, _, artifact = setup_entry(tmp_path, monkeypatch)
    metadata = artifact.meta_path.read_text()
    artifact.meta_path.unlink()
    with inference_scope():
        assert runtime.should_buy({}) is None
        artifact.meta_path.write_text(metadata)
        assert runtime.should_buy({}) is None
        assert not runtime.entry_prediction_state()["activation_ready"]
    with inference_scope():
        assert runtime.should_buy({}) == .5
        assert runtime.entry_prediction_state()["activation_ready"]


def test_actual_specialized_regression_version_and_interval_stay_together(tmp_path, monkeypatch):
    from analytics import model_runtime_common as runtime
    from ml.family_training import train_regressor_family
    from test_financial_model_acceptance import frame
    from net_financial_fixtures import net_frame
    report = train_regressor_family(family="ev", targets=["ev_realized"],
        feature_set_name="ev_features", frame=net_frame(frame()),
        output_dir=tmp_path / "ml" / "models" / "ev")
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    path = Path(report["targets"]["ev_realized"]["model_path"])
    with inference_scope():
        before = runtime.predict_regression_estimate("ev", "ev_realized", {"price_pct_5m": 80})
        assert before["value"] == pytest.approx(200)
        path.write_bytes(b"corrupt replacement")
        runtime.invalidate_model_cache(path)
        assert runtime.predict_regression_estimate("ev", "ev_realized", {"price_pct_5m": 80}) == before
        assert runtime.predict_regression_estimate("ev", "ev_realized", {"price_pct_5m": 5})["value"] == pytest.approx(-60)
    assert runtime.predict_model("ev", "ev_realized", {}) is None


def test_family_manifest_path_does_not_change_mid_entry(tmp_path, monkeypatch):
    from analytics import model_runtime_common as runtime
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    directory = tmp_path / "ml" / "models" / "runner"
    directory.mkdir(parents=True)
    manifest = directory / "advisory_manifest.json"
    def publish(version):
        manifest.write_text(json.dumps({"role": "scanner_ranking_only",
            "heads": {"runner_10000": {"path": f"versions/{version}/runner_10000.pkl"}}}))
    publish("one")
    with inference_scope():
        before = runtime._model_path("runner", "runner_10000")
        publish("version_two_longer")
        assert runtime._model_path("runner", "runner_10000") == before
    assert "version_two_longer" in str(runtime._model_path("runner", "runner_10000"))


def policy_case(monkeypatch, metadata, *, lane="pump_early_pumpswap_profit", proba=.5):
    from analytics import ml_policy as policy
    monkeypatch.setattr(policy, "CFG", SimpleNamespace(ML_GATE_MODE="enforce", ML_SIZING_ENABLED=False))
    monkeypatch.setattr(policy, "_read_json", lambda path: (_ for _ in ()).throw(AssertionError("stale threshold file read")))
    return policy.decide_ml_action(token={"entry_lane": lane}, feature_row={}, proba=proba,
        base_rules_passed=True, dry_run=True, live=False, entry_model_activation_ready=True,
        entry_model_metadata=metadata)


def test_runtime_threshold_is_bound_to_model_metadata_and_lane(monkeypatch):
    metadata = {"activation_ready": True, "ai_threshold_recommended": .4,
        "thresholds_by_lane": {"by_lane": {"pump_early_pumpswap_profit":
            {"threshold": .6, "activation_ready": True}}}}
    result = policy_case(monkeypatch, metadata)
    assert not result.allow_buy and result.enforce and result.threshold == .6
    assert result.source == "checked_model_snapshot"
    metadata["thresholds_by_lane"]["by_lane"]["pump_early_pumpswap_profit"]["threshold"] = None
    metadata["thresholds_by_lane"]["by_lane"]["pump_early_pumpswap_profit"]["picked"] = .3
    assert policy_case(monkeypatch, metadata).allow_buy


@pytest.mark.parametrize("value", [None, True, False, float("nan"), float("inf"), -.1, 1.1, "invalid"])
def test_invalid_or_missing_snapshot_threshold_is_neutral(monkeypatch, value):
    result = policy_case(monkeypatch, {"activation_ready": True, "ai_threshold_recommended": value})
    assert result.allow_buy and not result.activation_ready and not result.enforce
    assert result.threshold is None


@pytest.mark.parametrize("value", [None, True, False, float("nan"), float("inf"), -.1, 1.1, "invalid"])
def test_unknown_probability_is_not_an_enforced_losing_prediction(monkeypatch, value):
    result = policy_case(monkeypatch, {"activation_ready": True, "ai_threshold_recommended": .5}, proba=value)
    assert result.allow_buy and not result.activation_ready and result.proba is None


def load_helpers():
    source = Path("run_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {"_score_entry_inputs", "_entry_ml_allowed", "_refresh_entry_sizing", "_defer_entry_model"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"dt": dt}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<actual-entry-helpers>", "exec"), namespace)
    return namespace, tree


def test_current_vector_is_used_by_all_heads_ranking_and_policy_with_fixed_clock():
    from features.builder import build_feature_vector
    ns, _ = load_helpers()
    seen = []
    ns.update(CFG=SimpleNamespace(ML_GATE_MODE="enforce"), DRY_RUN=True, AI_THRESHOLD=.5,
        _runner_profile_for_subject=lambda token: "uncapped", _config_hash=lambda: "fixed",
        build_feature_vector=build_feature_vector,
        should_buy=lambda vec: seen.append(("entry", vec.to_dict())) or float(vec["green_sniper_score"]) / 100,
        predict_risk=lambda vec: seen.append(("risk", vec.to_dict())) or float(vec["route_proxy"]),
        predict_ev=lambda vec: seen.append(("ev", vec.to_dict())) or float(vec["price_pct_5m"]),
        entry_prediction_state=lambda: {"activation_ready": True, "metadata": {"revision": 1}},
        decide_ml_action=lambda **kw: seen.append(("policy", deepcopy(kw))) or SimpleNamespace(threshold=.4),
        research_runtime=SimpleNamespace(score_candidate=lambda payload, **kw: seen.append(("rank", deepcopy(payload), kw)) or kw),
        filters=SimpleNamespace(effective_ai_threshold=lambda *a: .5))
    now = dt.datetime(2026, 10, 7, 22, tzinfo=dt.timezone.utc)
    token = {"address": "synthetic", "created_at": (now - dt.timedelta(minutes=4)).isoformat(),
        "price_usd": 1, "price_pct_5m": 25000, "green_sniper_score": 20, "route_proxy": 0}
    first = ns["_score_entry_inputs"](token, captured_at=now)
    token.update(entry_lane="pump_early_paper_bootstrap_micro", green_sniper_score=80, route_proxy=1)
    second = ns["_score_entry_inputs"](token, captured_at=now)
    assert first[2:5] == (.2, 0, 25000) and second[2:5] == (.8, 1, 25000)
    assert first[1]["age_minutes"] == second[1]["age_minutes"] == 4
    for offset, bundle in [(0, first), (5, second)]:
        for n in range(3):
            assert seen[offset + n][1] == bundle[1]
        assert seen[offset + 3][1]["feature_row"] == bundle[1]
        assert seen[offset + 3][1]["proba"] == bundle[2]
        assert seen[offset + 3][1]["entry_model_metadata"] == {"revision": 1}
        assert seen[offset + 4][1] == bundle[1]
        assert seen[offset + 4][2]["proba"] == bundle[2]


@pytest.mark.parametrize("paper,bypass,enforced,allowed,expected", [
    (True, True, False, False, True), (False, True, False, False, False),
    (True, False, False, False, False), (True, True, True, False, False),
    (True, True, True, True, False), (False, False, False, True, True)])
def test_only_explicit_paper_exploration_bypasses_nonrisk_ml(paper, bypass, enforced, allowed, expected):
    ns, _ = load_helpers()
    decision = SimpleNamespace(allow_buy=allowed, risk_veto_enforced=enforced)
    assert ns["_entry_ml_allowed"](decision, paper=paper, bypass=bypass) is expected


def test_refreshed_sizing_keeps_strategy_and_paper_probe_caps():
    ns, _ = load_helpers()
    calls = []
    ns.update(entry_sizing=SimpleNamespace(compute_entry_sizing=lambda **kw: calls.append(kw) or [1.]),
        TRADE_AMOUNT_SOL_CFG=.1, DRY_RUN=True, _PAPER_COLD_START_SHADOW_PROBE_SIZE_MULTIPLIER=.2,
        _apply_strategy_size_cap=lambda size, cap: size + [cap],
        compute_green_sniper_sizing=lambda token, **kw: calls.append(kw) or SimpleNamespace(mode="current", reason="new", amount_sol=.1))
    token = {"entry_lane": "pump_early_green_candle_sniper"}
    size, green = ns["_refresh_entry_sizing"](token, None, .5, .8, -20,
        queue_attempts=2, strategy_cap=.4, paper_shadow_probe=True)
    assert size == [1., .4, .2] and green.mode == "current"
    assert calls[0]["ai_proba"] is None and calls[1]["risk_proba"] == .8
    assert calls[1]["ev_pred_pct"] == -20 and token["green_sniper_size_mode"] == "current"


def final_guard_namespace(*, paper=True, veto=False, allow=True, quality=True, amount=.1, current_amount=.1):
    ns, tree = load_helpers()
    evaluate = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_evaluate_and_buy")
    limiter = next(i for i, n in enumerate(evaluate.body) if isinstance(n, ast.If) and "_BUY_LIMITER.allow" in ast.unparse(n.test))
    start = max(i for i, n in enumerate(evaluate.body[:limiter]) if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call) and getattr(n.value.func, "id", "") == "_score_entry_inputs")
    nodes = evaluate.body[start:limiter]
    wrapper = ast.parse("async def exercise():\n    pass\n").body[0]
    wrapper.body = nodes + ast.parse("return 'admitted'").body
    calls = []
    final_vec = {"address": "synthetic", "route_proxy": 1, "green_sniper_score": 90}
    decision = SimpleNamespace(allow_buy=allow, risk_veto_enforced=veto, reason="current_reject")
    size = SimpleNamespace(regime="pump_early", quality_points=80, multiplier=1)
    ns.update(token={"address": "synthetic"}, addr="synthetic", entry_model_at=object(), entry_observation=object(),
        queue_attempts=0, strategy_decision=SimpleNamespace(size_cap_multiplier=.5, action="live"),
        paper_shadow_probe_live=False, closed_trades_for_gate=0, DRY_RUN=paper,
        moonshot_fast_path=False, shadow_followup_fast_path=False, paper_exploration_fast_path=False,
        paper_bootstrap_fast_path=False, sniper_micro_fallback_probe=False, sniper_micro_fallback_fast_path=False,
        amount_sol=amount, CFG=SimpleNamespace(LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED=True),
        _score_entry_inputs=lambda *a, **kw: (final_vec, final_vec, .9, .8, 10, .5, {"rank_score": 90}, decision),
        _refresh_entry_sizing=lambda *a, **kw: (size, None),
        _entry_quality_gate=lambda *a, **kw: calls.append(("quality", kw)) or (quality, "current_quality"),
        _paper_cold_start_active=lambda n: False,
        _compute_trade_amount=lambda mult: current_amount,
        _defer_entry_model=lambda *a, **kw: calls.append(("deferred", kw)),
        _entry_observation_is_current=lambda *a, **kw: calls.append(("observation", kw)) or True,
        log_ml_policy_decision_event=lambda *a, **kw: None,
        _pending_ai_vectors={})
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "<actual-final-guard>", "exec"), ns)
    return ns, calls, final_vec


@pytest.mark.parametrize("veto,allow,quality", [(True, True, True), (False, False, True), (False, True, False)])
def test_actual_final_guard_rejects_current_model_or_quality_before_buy(veto, allow, quality):
    ns, calls, _ = final_guard_namespace(veto=veto, allow=allow, quality=quality)
    assert asyncio.run(ns["exercise"]()) is None
    assert any(kind == "deferred" for kind, _ in calls)
    assert not ns["_pending_ai_vectors"]
    assert not any(kind == "observation" for kind, _ in calls)


def test_actual_final_guard_saves_exact_current_scored_vector():
    ns, calls, vec = final_guard_namespace()
    assert asyncio.run(ns["exercise"]()) == "admitted"
    assert ns["_pending_ai_vectors"]["synthetic"] is vec
    assert calls[-1][1]["vector"] == vec
    assert calls[0][1]["rank_info"] == {"rank_score": 90}


def test_actual_final_guard_retries_lower_live_size_without_skipping_route_checks():
    ns, calls, _ = final_guard_namespace(paper=False, current_amount=.05)
    assert asyncio.run(ns["exercise"]()) is None
    assert calls[-1][1]["reason"] == "ml_policy:current_sizing_requires_revalidation"
    assert not ns["_pending_ai_vectors"]


def test_guarded_production_entry_always_owns_inference_scope():
    _, tree = load_helpers()
    guarded = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_evaluate_and_buy_guarded")
    scopes = [ast.unparse(item.context_expr) for n in ast.walk(guarded) if isinstance(n, ast.With) for item in n.items]
    assert "inference_scope()" in scopes
    evaluate = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_evaluate_and_buy")
    # No vector-only rebuilds between first ML scoring and execution may reuse old predictions.
    start = next(i for i, n in enumerate(evaluate.body) if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call) and getattr(n.value.func, "id", "") == "_score_entry_inputs")
    for node in ast.walk(ast.Module(body=evaluate.body[start:], type_ignores=[])):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "vec" for t in node.targets):
            assert not (isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "build_feature_vector")


def test_missing_prediction_is_explicit_unknown_in_heuristic_rank():
    from analytics.research_runtime import score_candidate
    missing = score_candidate({}, proba=None, threshold=.1)
    zero = score_candidate({}, proba=0, threshold=.1)
    assert missing["ml_prediction_status"] == "unknown" and missing["components"]["ml"] == 0
    assert zero["ml_prediction_status"] == "observed" and zero["components"]["ml"] > 0


def test_actual_primary_predicts_again_only_for_consumed_matrix_changes(tmp_path, monkeypatch):
    from test_financial_model_acceptance import setup_entry
    runtime, _, _ = setup_entry(tmp_path, monkeypatch)
    model, _, _ = runtime._load_model()
    calls = []
    original = model.predict_proba
    monkeypatch.setattr(model, "predict_proba", lambda X: calls.append(X.copy()) or original(X))
    with inference_scope():
        assert runtime.should_buy({"price_pct_5m": 5, "entry_lane": "before"}) == .5
        assert runtime.should_buy({"price_pct_5m": "5.0", "entry_lane": "after"}) == .5
        assert len(calls) == 1
        assert runtime.should_buy({"price_pct_5m": 80}) == .5
        assert len(calls) == 2
    assert runtime.should_buy({"price_pct_5m": 80}) == .5
    assert len(calls) == 3


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True, -.1, 1.1])
def test_unknown_prediction_does_not_fabricate_ai_sizing_points(monkeypatch, value):
    from analytics import sizing
    monkeypatch.setattr(sizing, "AI_SIZING_ENABLED", True)
    _, notes = sizing._quality_points({}, value, .1)
    assert not any(note.startswith("ai_edge_") for note in notes)


def test_sizing_uses_same_snapshot_threshold_as_admission(monkeypatch):
    from analytics import sizing
    monkeypatch.setattr(sizing, "AI_SIZING_ENABLED", True)
    high_points, high_notes = sizing._quality_points({}, .8, .9)
    low_points, low_notes = sizing._quality_points({}, .8, .5)
    assert low_points == high_points + 2
    assert "ai_edge_strong" in low_notes and not any(n.startswith("ai_edge_") for n in high_notes)


def test_actual_telemetry_keeps_unknown_distinct_from_zero(monkeypatch):
    from utils import runtime_telemetry as telemetry
    calls = []
    monkeypatch.setattr(telemetry, "record_runtime_event", lambda *args, **kw: calls.append(kw))
    for value in (None, 0., .9):
        telemetry.log_ml_decision_event("synthetic", proba=value, threshold=.5,
            passed=False, enforced=False, gate_mode="shadow")
    assert [c["proba"] for c in calls] == [None, 0., .9]
    assert [c["prediction_status"] for c in calls] == ["unknown", "observed", "observed"]


def test_actual_position_builder_keeps_unknown_probability_nullable():
    from analytics import runner_ladder
    ns, tree = load_helpers()
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_build_entry_position")
    # Exercise its complete production constructor with an isolated receipt object.
    ns.update(Position=lambda **kw: SimpleNamespace(**kw), runner_ladder=runner_ladder,
        _runner_profile_for_subject=lambda token: "uncapped",
        get_runtime_context=lambda: {}, parse_iso_utc=lambda value: value,
        utc_now=lambda: dt.datetime(2026, 10, 7, tzinfo=dt.timezone.utc), DRY_RUN=True,
        _metric_int=lambda token, key: 0, _is_liquidity_proxy=lambda token: False,
        _config_hash=lambda: "synthetic")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<actual-position-builder>", "exec"), ns)
    size = SimpleNamespace(regime="pump_early", bucket="standard", multiplier=1.)
    unknown = ns["_build_entry_position"]({}, size, addr="synthetic", amount_sol=.1, proba=None)
    zero = ns["_build_entry_position"]({}, size, addr="synthetic", amount_sol=.1, proba=0.)
    assert unknown.entry_ai_proba is None and zero.entry_ai_proba == 0
    assert unknown.buy_amount_sol == zero.buy_amount_sol == .1


def test_final_rejection_is_wait_not_a_losing_learning_label():
    ns, _ = load_helpers()
    calls = []
    ns.update(_pending_ai_vectors={"synthetic": {"old": 1}},
        log_ml_policy_decision_event=lambda *a, **kw: calls.append(("ml", kw)),
        _research_decision=lambda *a, **kw: calls.append(("research", kw)),
        _requeue_or_cooldown_candidate=lambda *a, **kw: calls.append(("retry", kw)),
        _DEX_MATURE_QUALITY_BACKOFF_S=60)
    ns["_defer_entry_model"]({"address": "synthetic"}, decision=object(), proba=None,
        threshold=.5, rank_info={}, reason="current_reject")
    assert not ns["_pending_ai_vectors"]
    research = next(kw for kind, kw in calls if kind == "research")
    assert research["action"] == "wait" and research["proba"] is None
    assert [kind for kind, _ in calls] == ["ml", "research", "retry"]
