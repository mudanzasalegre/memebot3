# AutoResearch Runbook

## Current Block

This block installs the base contract:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_research_safety.py tests\test_autoresearch_schema.py tests\test_autoresearch_objectives.py
.\.venv\Scripts\python.exe scripts\strategy_quality_gate.py --warn-only
```

## Candidate Policy Lifecycle

1. Create a candidate policy JSON.
2. Validate it with `validate_candidate_policy()`.
3. Validate safety with `validate_candidate_safety()`.
4. Compare replay metrics with `calculate_objective_score()`.
5. Keep only candidates with safety OK, hard gates passed, and positive score.

## Paper-Only Requirements

Before any future paper-forward work, verify:

```text
DRY_RUN=1
STRATEGY_OPTIMIZATION_LOCK=true
LIVE_CANARY_ENABLED=false
GREEN_SNIPER_LIVE_ENABLED=false
RESEARCH_RANK_CANARY_LIVE_ENABLED=false
AUTO_PROMOTE_LIVE=false
MODEL_AUTO_PROMOTE=false
LLM_TRADING_ENABLED=false
```

## Replay Requirements

Replay must use only local files under `data/`, `logs/`, and report artifacts. It
must not call DexScreener, GeckoTerminal, Birdeye, Jupiter, Pump.fun,
PumpPortal, RugCheck, Helius, or RPC endpoints.

## Rejection Reasons

Reject candidates when they:

- Touch forbidden env keys or secrets.
- Turn on live or auto-promotion flags.
- Change provider cadence/RPM keys.
- Exceed paper sizing caps.
- Increase API 429s.
- Increase severe losses or liquidity crush count.
- Reduce median PnL below the configured hard gate.
- Worsen drawdown above the configured hard gate.
