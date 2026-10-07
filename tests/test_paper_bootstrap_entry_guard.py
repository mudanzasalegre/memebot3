from __future__ import annotations

import ast
import asyncio
import textwrap
from pathlib import Path
from types import SimpleNamespace


def _load_entry_lane_guard(*, bootstrap_allowed: bool):
    source = Path("run_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="run_bot.py")
    evaluate = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_evaluate_and_buy"
    )
    guard = next(
        node
        for node in ast.walk(evaluate)
        if isinstance(node, ast.If) and "REQUIRE_ENTRY_LANE_FOR_BUY" in ast.unparse(node.test)
    )
    guard_source = textwrap.indent(ast.unparse(guard), "    ")
    wrapper = (
        "async def exercise(token, ses):\n"
        "    addr = token['address']\n"
        "    proba = 0.75\n"
        "    entry_model_at = None\n"
        "    ai_threshold_eff = 0.5\n"
        "    rank_info = {'source': 'before'}\n"
        "    size_decision = SimpleNamespace(regime='pump_early')\n"
        "    paper_bootstrap_decision = None\n"
        "    paper_bootstrap_fast_path = False\n"
        "    require_jup_for_buy = False\n"
        f"{guard_source}\n"
        "    return {\n"
        "        'continued': True,\n"
        "        'paper_bootstrap_fast_path': paper_bootstrap_fast_path,\n"
        "        'rank_info': rank_info,\n"
        "        'require_jup_for_buy': require_jup_for_buy,\n"
        "    }\n"
    )

    calls: list[tuple[str, object]] = []

    async def maybe_apply(token, ses, addr, *, trigger_stage, trigger_reason):
        calls.append(("bootstrap", (trigger_stage, trigger_reason)))
        if bootstrap_allowed:
            token["entry_lane"] = "pump_early_paper_bootstrap_micro"
        return SimpleNamespace(allowed=bootstrap_allowed)

    async def open_shadow(*args, **kwargs):
        calls.append(("open_shadow", kwargs.get("stage")))

    def record(name):
        def inner(*args, **kwargs):
            calls.append((name, kwargs.get("stage") or kwargs.get("reason")))

        return inner

    def score_inputs(token, *, captured_at):
        payload = dict(token)
        calls.append(("score_inputs", token.get("entry_lane")))
        return payload, payload, .75, None, None, .5, {"source": "after"}, None

    namespace = {
        "CFG": SimpleNamespace(
            REQUIRE_ENTRY_LANE_FOR_BUY=True,
            PAPER_BOOTSTRAP_REQUIRE_ROUTE=True,
            UNTAGGED_BUY_SHADOW_ENABLED=True,
        ),
        "REASON_UNTAGGED_BLOCKED": "untagged_buy_blocked",
        "SimpleNamespace": SimpleNamespace,
        "_stats": {"filtered_out": 0},
        "evaluate_untagged_buy_guard": lambda token: SimpleNamespace(
            allowed=False,
            reason="untagged_buy_blocked",
        ),
        "apply_untagged_breakout_context": record("breakout_context"),
        "apply_untagged_buy_shadow_context": record("shadow_context"),
        "_maybe_apply_paper_bootstrap": maybe_apply,
        "build_feature_vector": lambda token: dict(token),
        "_score_entry_inputs": score_inputs,
        "research_runtime": SimpleNamespace(
            score_candidate=lambda payload, **kwargs: calls.append(("score", payload["entry_lane"]))
            or {"source": "after"}
        ),
        "_store_policy_reject": record("policy_reject"),
        "_research_decision": record("research_decision"),
        "_open_shadow": open_shadow,
        "_stream_candidate_cooldown_s": lambda token, outcome: 300,
        "_remember_stream_candidate_cooldown": record("cooldown"),
        "_remove_from_queue_if_present": record("remove"),
    }
    exec(compile(wrapper, "<entry_lane_guard>", "exec"), namespace)
    return namespace["exercise"], calls, namespace["_stats"]


def test_eligible_untagged_candidate_enters_bootstrap_and_continues() -> None:
    exercise, calls, stats = _load_entry_lane_guard(bootstrap_allowed=True)

    result = asyncio.run(exercise({"address": "eligible", "price_usd": 1.0}, object()))

    assert result == {
        "continued": True,
        "paper_bootstrap_fast_path": True,
        "rank_info": {"source": "after"},
        "require_jup_for_buy": True,
    }
    assert ("bootstrap", ("entry_lane_guard", "untagged_buy_blocked")) in calls
    assert ("score", "pump_early_paper_bootstrap_micro") in calls
    assert not any(name in {"shadow_context", "open_shadow", "remove"} for name, _ in calls)
    assert stats["filtered_out"] == 0


def test_rejected_untagged_candidate_keeps_shadow_and_returns() -> None:
    exercise, calls, stats = _load_entry_lane_guard(bootstrap_allowed=False)

    result = asyncio.run(exercise({"address": "rejected", "price_usd": 1.0}, object()))

    assert result is None
    assert ("bootstrap", ("entry_lane_guard", "untagged_buy_blocked")) in calls
    assert any(name == "shadow_context" for name, _ in calls)
    assert ("research_decision", "entry_lane_guard") in calls
    assert ("open_shadow", "entry_lane_guard") in calls
    assert any(name == "remove" for name, _ in calls)
    assert not any(name == "score" for name, _ in calls)
    assert stats["filtered_out"] == 1
