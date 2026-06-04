from __future__ import annotations

from types import SimpleNamespace

import scripts.strategy_quality_gate as gate


def _safe_cfg(**overrides):
    values = {
        "STRATEGY_OPTIMIZATION_LOCK": True,
        "DRY_RUN": True,
        "LIVE_CANARY_ENABLED": False,
        "GREEN_SNIPER_LIVE_ENABLED": False,
        "RESEARCH_RANK_CANARY_LIVE_ENABLED": False,
        "LATE_MOMENTUM_WATCH_LIVE_ENABLED": False,
        "LIVE_AGGRESSIVE_TRADING_ENABLED": False,
        "AUTO_PROMOTE_LIVE": False,
        "MODEL_AUTO_PROMOTE": False,
        "ML_AUTO_PROMOTE_LANES": False,
        "ML_ALLOW_RESEARCH_LIVE": False,
        "ML_ALLOW_UNKNOWN_LIVE": False,
        "ALLOW_LIVE_POLICY_ENFORCE": False,
        "AUTORESEARCH_LIVE_PROMOTION_ENABLED": False,
        "AUTORESEARCH_AUTO_LIVE_PROMOTE": False,
        "AUTORESEARCH_LLM_CAN_TOUCH_LIVE": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _candidate(live_allowed: bool = False) -> dict:
    return {
        "proposal_id": "ar_gate_001",
        "created_at_utc": "2026-06-04T00:00:00+00:00",
        "experiment_type": "replay",
        "hypothesis": "Gate test candidate",
        "target_lanes": ["pump_early_moonshot_micro_lottery"],
        "changes": {"MOONSHOT_MICRO_CONFIRMATION_PNL": "75"},
        "expected_effect": {"increase_pnl": True},
        "required_gates": ["replay_positive", "api_budget_ok"],
        "api_budget_sensitive": True,
        "live_allowed": live_allowed,
        "risk_notes": ["paper only"],
    }


def _write_autoresearch_contract(root, *, candidate_live: bool = False, paper_profile: bool = True, scoreboard: bool = True) -> None:
    research = root / "research_loop"
    research.mkdir(parents=True, exist_ok=True)
    for name in ("safety.yaml", "safety.py", "objectives.yaml", "objectives.py", "experiment_schema.py"):
        (research / name).write_text("# test\n", encoding="utf-8")
    proposals = root / "strategy_proposals"
    proposals.mkdir(parents=True, exist_ok=True)
    (proposals / "schema.autoresearch.json").write_text("{}", encoding="utf-8")
    candidates = proposals / "candidates"
    candidates.mkdir(parents=True, exist_ok=True)
    (candidates / "ar_gate_001.json").write_text(json_dumps(_candidate(live_allowed=candidate_live)), encoding="utf-8")
    runs = root / "data" / "research_runs"
    runs.mkdir(parents=True, exist_ok=True)
    if scoreboard:
        (runs / "scoreboard.json").write_text('{"entries":[]}', encoding="utf-8")
    (runs / "api_budget.json").write_text(
        '{"comparison":{"ok":true,"deltas":{"api_429_count":0,"provider_degraded_minutes":0},"rejection_reasons":[]}}',
        encoding="utf-8",
    )
    if paper_profile:
        profiles = root / "config" / "profiles"
        profiles.mkdir(parents=True, exist_ok=True)
        (profiles / "paper_research_candidate_ar_gate_001.env").write_text(
            "DRY_RUN=1\n"
            "LIVE_CANARY_ENABLED=false\n"
            "AUTORESEARCH_LIVE_PROMOTION_ENABLED=false\n"
            "AUTORESEARCH_AUTO_LIVE_PROMOTE=false\n"
            "AUTORESEARCH_LLM_CAN_TOUCH_LIVE=false\n",
            encoding="utf-8",
        )


def json_dumps(payload) -> str:
    import json

    return json.dumps(payload, sort_keys=True)


def test_strategy_quality_gate_returns_list() -> None:
    assert isinstance(gate.checks(), list)


def test_optimization_lock_blocks_live_flags(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "provider_health_snapshot", lambda: {"overall_status": "ok"})
    blocked_flags = (
        "LIVE_CANARY_ENABLED",
        "GREEN_SNIPER_LIVE_ENABLED",
        "RESEARCH_RANK_CANARY_LIVE_ENABLED",
        "LATE_MOMENTUM_WATCH_LIVE_ENABLED",
        "LIVE_AGGRESSIVE_TRADING_ENABLED",
        "AUTO_PROMOTE_LIVE",
        "MODEL_AUTO_PROMOTE",
        "ML_AUTO_PROMOTE_LANES",
        "ML_ALLOW_RESEARCH_LIVE",
        "ML_ALLOW_UNKNOWN_LIVE",
        "ALLOW_LIVE_POLICY_ENFORCE",
    )
    for flag in blocked_flags:
        monkeypatch.setattr(
            gate,
            "CFG",
            SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=True, DRY_RUN=True, **{flag: True}),
        )
        errors = gate.checks()
        assert f"STRATEGY_OPTIMIZATION_LOCK=true blocks {flag}=true" in errors


def test_optimization_lock_blocks_non_dry_run(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=True, DRY_RUN=False))

    assert "STRATEGY_OPTIMIZATION_LOCK=true requires DRY_RUN=true" in gate.checks()


def test_autoresearch_runtime_flags_must_remain_false(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "CFG", _safe_cfg(AUTORESEARCH_AUTO_LIVE_PROMOTE=True))

    assert "AUTORESEARCH_AUTO_LIVE_PROMOTE must remain false" in gate.checks()


def test_autoresearch_contract_accepts_safe_artifacts(monkeypatch, tmp_path) -> None:
    _write_autoresearch_contract(tmp_path)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "CFG", _safe_cfg())

    assert gate.checks() == []


def test_autoresearch_contract_requires_scoreboard_and_paper_profile(monkeypatch, tmp_path) -> None:
    _write_autoresearch_contract(tmp_path, paper_profile=False, scoreboard=False)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "CFG", _safe_cfg())

    errors = gate.checks()

    assert "autoresearch scoreboard missing: data/research_runs/scoreboard.json" in errors
    assert "autoresearch paper profile missing: config/profiles/paper_research_candidate_*.env" in errors


def test_autoresearch_contract_rejects_live_allowed_candidate(monkeypatch, tmp_path) -> None:
    _write_autoresearch_contract(tmp_path, candidate_live=True)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "CFG", _safe_cfg())

    errors = gate.checks()

    assert any("autoresearch candidate invalid" in error and "live_allowed_must_be_false" in error for error in errors)


def test_optimization_lock_allows_paper_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(
        gate,
        "CFG",
        SimpleNamespace(
            STRATEGY_OPTIMIZATION_LOCK=True,
            DRY_RUN=True,
            LIVE_CANARY_ENABLED=False,
            GREEN_SNIPER_LIVE_ENABLED=False,
            RESEARCH_RANK_CANARY_LIVE_ENABLED=False,
            LATE_MOMENTUM_WATCH_LIVE_ENABLED=False,
            LIVE_AGGRESSIVE_TRADING_ENABLED=False,
            AUTO_PROMOTE_LIVE=False,
            MODEL_AUTO_PROMOTE=False,
            ML_AUTO_PROMOTE_LANES=False,
            ML_ALLOW_RESEARCH_LIVE=False,
            ML_ALLOW_UNKNOWN_LIVE=False,
            ALLOW_LIVE_POLICY_ENFORCE=False,
        ),
    )

    assert gate.checks() == []


def test_quality_gate_blocks_broad_pumpswap_without_strict(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(
        gate,
        "CFG",
        SimpleNamespace(
            STRATEGY_OPTIMIZATION_LOCK=False,
            PUMP_EARLY_PROFIT_LANE_ENABLED=True,
            PUMPSWAP_PRIME_STRICT_ENABLED=False,
            LIVE_CANARY_ENABLED=False,
            AUTO_PROMOTE_LIVE=False,
            MODEL_AUTO_PROMOTE=False,
            LLM_TRADING_ENABLED=False,
            SOCIALS_HOT_PATH_BLOCKING=False,
            GREEN_SNIPER_REQUIRE_SOCIALS=False,
            GREEN_SNIPER_POLICY_MODE="shadow",
            LATE_MOMENTUM_POLICY_MODE="shadow",
            RESEARCH_RANK_POLICY_MODE="shadow",
            GREEN_SNIPER_LIVE_ENABLED=False,
            PAPER_SNIPER_MODE=False,
            GREEN_SNIPER_ENABLED=False,
        ),
    )

    assert "PUMP_EARLY_PROFIT_LANE_ENABLED=true requires PUMPSWAP_PRIME_STRICT_ENABLED=true" in gate.checks()


def test_quality_gate_blocks_hotfix_live_surfaces(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(
        gate,
        "CFG",
        SimpleNamespace(
            STRATEGY_OPTIMIZATION_LOCK=False,
            LIVE_CANARY_ENABLED=False,
            AUTO_PROMOTE_LIVE=False,
            MODEL_AUTO_PROMOTE=False,
            POST_PARTIAL_PROTECTION_LIVE_ENABLED=True,
            BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED=True,
            RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED=True,
            BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED=True,
            PUMP_EARLY_PROFIT_LANE_ENABLED=True,
            PUMPSWAP_PRIME_STRICT_ENABLED=True,
            PUMPSWAP_PRIME_SHADOW_IF_NOT_STRICT=True,
            LLM_TRADING_ENABLED=False,
            SOCIALS_HOT_PATH_BLOCKING=False,
            GREEN_SNIPER_REQUIRE_SOCIALS=False,
            GREEN_SNIPER_POLICY_MODE="shadow",
            LATE_MOMENTUM_POLICY_MODE="shadow",
            RESEARCH_RANK_POLICY_MODE="shadow",
            GREEN_SNIPER_LIVE_ENABLED=False,
            PAPER_SNIPER_MODE=False,
            GREEN_SNIPER_ENABLED=False,
        ),
    )

    errors = gate.checks()

    assert "POST_PARTIAL_PROTECTION_LIVE_ENABLED must remain false" in errors
    assert "BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED must remain false" in errors
    assert "RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED must remain false" in errors
    assert "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED must remain false" in errors


def test_live_canary_enabled_is_read_from_cfg(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(
        gate,
        "CFG",
        SimpleNamespace(
            STRATEGY_OPTIMIZATION_LOCK=False,
            DRY_RUN=False,
            LIVE_CANARY_ENABLED=True,
            LIVE_CANARY_MAX_OPEN=1,
            LIVE_CANARY_MAX_DAILY_BUYS=3,
            LIVE_CANARY_DAILY_LOSS_CAP_SOL=0.05,
            LIVE_REQUIRE_ROUTE=True,
            LIVE_REQUIRE_PROVIDER_HEALTH=True,
            LIVE_CANARY_MANUAL_APPROVAL=False,
        ),
    )

    errors = gate.checks()

    assert "LIVE_CANARY requires LIVE_CANARY_MANUAL_APPROVAL=true" in errors


def test_paper_rank_research_profile_is_validated(monkeypatch, tmp_path) -> None:
    profile_dir = tmp_path / "config" / "profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "paper_rank_research_v1.env").write_text(
        "\n".join(
            [
                "DRY_RUN=1",
                "PAPER_SNIPER_MODE=true",
                "STRATEGY_OPTIMIZATION_LOCK=true",
                "LIVE_CANARY_ENABLED=false",
                "AUTO_PROMOTE_LIVE=false",
                "MODEL_AUTO_PROMOTE=false",
                "ML_AUTO_PROMOTE_LANES=false",
                "ML_ALLOW_RESEARCH_LIVE=false",
                "ML_ALLOW_UNKNOWN_LIVE=false",
                "ALLOW_LIVE_POLICY_ENFORCE=false",
                "RESEARCH_RANK_CANARY_ENABLED=true",
                "RESEARCH_RANK_CANARY_PAPER_ENABLED=true",
                "RESEARCH_RANK_CANARY_LIVE_ENABLED=false",
                "RESEARCH_RANK_CANARY_MIN_SCORE=0.647",
                "RESEARCH_RANK_CANARY_MIN_PRICE5M=40",
                "RESEARCH_RANK_CANARY_PREFER_REAL_LIQUIDITY=true",
                "GREEN_SNIPER_POLICY_MODE=shadow",
                "GREEN_SNIPER_BUY_RESTRICTED_ENABLED=true",
                "GREEN_SNIPER_LIVE_ENABLED=false",
                "LATE_MOMENTUM_WATCH_BUY_ENABLED=false",
                "LATE_MOMENTUM_WATCH_RESEARCH_ENABLED=true",
                "LATE_MOMENTUM_WATCH_AUTORESEARCH_ENABLED=false",
                "LATE_MOMENTUM_WATCH_LIVE_ENABLED=false",
                "POST_PARTIAL_PROTECTION_ENABLED=true",
                "POST_PARTIAL_PROTECTION_PAPER_ENABLED=true",
                "POST_PARTIAL_PROTECTION_LIVE_ENABLED=false",
                "SOCIALS_HOT_PATH_BLOCKING=false",
                "GREEN_SNIPER_REQUIRE_SOCIALS=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(
        gate,
        "CFG",
        SimpleNamespace(
            STRATEGY_OPTIMIZATION_LOCK=True,
            DRY_RUN=True,
            LIVE_CANARY_ENABLED=False,
            GREEN_SNIPER_LIVE_ENABLED=False,
            RESEARCH_RANK_CANARY_LIVE_ENABLED=False,
            LATE_MOMENTUM_WATCH_LIVE_ENABLED=False,
            LIVE_AGGRESSIVE_TRADING_ENABLED=False,
            AUTO_PROMOTE_LIVE=False,
            MODEL_AUTO_PROMOTE=False,
            ML_AUTO_PROMOTE_LANES=False,
            ML_ALLOW_RESEARCH_LIVE=False,
            ML_ALLOW_UNKNOWN_LIVE=False,
            ALLOW_LIVE_POLICY_ENFORCE=False,
        ),
    )

    assert gate.checks() == []


def test_paper_rank_research_profile_rejects_live_flags(monkeypatch, tmp_path) -> None:
    profile_dir = tmp_path / "config" / "profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "paper_rank_research_v1.env").write_text(
        "DRY_RUN=1\n"
        "PAPER_SNIPER_MODE=true\n"
        "STRATEGY_OPTIMIZATION_LOCK=true\n"
        "LIVE_CANARY_ENABLED=true\n"
        "AUTO_PROMOTE_LIVE=false\n"
        "MODEL_AUTO_PROMOTE=false\n"
        "ML_AUTO_PROMOTE_LANES=false\n"
        "ML_ALLOW_RESEARCH_LIVE=false\n"
        "ML_ALLOW_UNKNOWN_LIVE=false\n"
        "ALLOW_LIVE_POLICY_ENFORCE=false\n"
        "RESEARCH_RANK_CANARY_ENABLED=true\n"
        "RESEARCH_RANK_CANARY_PAPER_ENABLED=true\n"
        "RESEARCH_RANK_CANARY_LIVE_ENABLED=false\n"
        "RESEARCH_RANK_CANARY_MIN_SCORE=0.647\n"
        "RESEARCH_RANK_CANARY_MIN_PRICE5M=40\n"
        "RESEARCH_RANK_CANARY_PREFER_REAL_LIQUIDITY=true\n"
        "GREEN_SNIPER_POLICY_MODE=shadow\n"
        "GREEN_SNIPER_BUY_RESTRICTED_ENABLED=true\n"
        "GREEN_SNIPER_LIVE_ENABLED=false\n"
        "LATE_MOMENTUM_WATCH_BUY_ENABLED=false\n"
        "LATE_MOMENTUM_WATCH_RESEARCH_ENABLED=true\n"
        "LATE_MOMENTUM_WATCH_AUTORESEARCH_ENABLED=false\n"
        "LATE_MOMENTUM_WATCH_LIVE_ENABLED=false\n"
        "POST_PARTIAL_PROTECTION_ENABLED=true\n"
        "POST_PARTIAL_PROTECTION_PAPER_ENABLED=true\n"
        "POST_PARTIAL_PROTECTION_LIVE_ENABLED=false\n"
        "SOCIALS_HOT_PATH_BLOCKING=false\n"
        "GREEN_SNIPER_REQUIRE_SOCIALS=false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False, LIVE_CANARY_ENABLED=False))

    errors = gate.checks()

    assert "paper_rank_research_v1 requires LIVE_CANARY_ENABLED=false" in errors


def test_model_enforcement_blocks_critical_training_warnings(monkeypatch, tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "model_training_report.json").write_text(
        '{"validation":{"critical_warnings":["in_sample_only","not_ready_for_enforcement"]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(
        gate,
        "CFG",
        SimpleNamespace(
            STRATEGY_OPTIMIZATION_LOCK=False,
            ML_GATE_MODE="enforce",
            LIVE_CANARY_ENABLED=False,
            AUTO_PROMOTE_LIVE=False,
            MODEL_AUTO_PROMOTE=False,
            LLM_TRADING_ENABLED=False,
            SOCIALS_HOT_PATH_BLOCKING=False,
            GREEN_SNIPER_REQUIRE_SOCIALS=False,
            GREEN_SNIPER_POLICY_MODE="shadow",
            LATE_MOMENTUM_POLICY_MODE="shadow",
            RESEARCH_RANK_POLICY_MODE="shadow",
            GREEN_SNIPER_LIVE_ENABLED=False,
            PAPER_SNIPER_MODE=False,
            GREEN_SNIPER_ENABLED=False,
        ),
    )

    errors = gate.checks()

    assert any("model enforcement blocked by critical warnings" in error for error in errors)


def test_shadow_model_mode_allows_critical_training_warnings(monkeypatch, tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "model_training_report.json").write_text(
        '{"validation":{"critical_warnings":["in_sample_only","not_ready_for_enforcement"]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(
        gate,
        "CFG",
        SimpleNamespace(
            STRATEGY_OPTIMIZATION_LOCK=False,
            ML_GATE_MODE="shadow",
            GREEN_SNIPER_ML_BLOCK_ENABLED=False,
            ML_GREEN_SNIPER_BLOCK_ENABLED=False,
            ML_RISK_VETO_ENABLED=False,
            LIVE_CANARY_ENABLED=False,
            AUTO_PROMOTE_LIVE=False,
            MODEL_AUTO_PROMOTE=False,
            LLM_TRADING_ENABLED=False,
            SOCIALS_HOT_PATH_BLOCKING=False,
            GREEN_SNIPER_REQUIRE_SOCIALS=False,
            GREEN_SNIPER_POLICY_MODE="shadow",
            LATE_MOMENTUM_POLICY_MODE="shadow",
            RESEARCH_RANK_POLICY_MODE="shadow",
            GREEN_SNIPER_LIVE_ENABLED=False,
            PAPER_SNIPER_MODE=False,
            GREEN_SNIPER_ENABLED=False,
        ),
    )

    assert not any("model enforcement blocked" in error for error in gate.checks())
