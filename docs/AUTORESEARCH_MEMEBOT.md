# AutoResearch for MemeBot3

AutoResearch is a replay-first research loop for improving MemeBot3 paper
strategy settings without allowing autonomous live trading.

## What It Optimizes

The system is designed to improve:

- Total PnL.
- Average and median PnL.
- Win rate.
- Runner capture.
- Moonshot peak capture.
- Shadow follow-up success.
- Rank canary profitability.

It penalizes:

- Severe losses.
- Liquidity crushes.
- Adverse ticks.
- No-pump exits.
- Drawdown.
- API 429s and provider degradation.
- Overtrading.
- Idle periods with no buys.

## Difference From `karpathy/autoresearch`

The original pattern edits code, runs a fixed experiment, and keeps or discards
the edit based on a validation metric. MemeBot3 keeps the same experimental
discipline but narrows the mutable surface to candidate policies and sandbox
profiles.

AutoResearch must not edit trading runtime code or bypass risk controls.

## Contract

```text
candidate_policy
-> safety validation
-> sandbox
-> replay
-> objective_score
-> accept/reject
-> paper-forward candidate
-> disabled live export for manual review only
```

## Absolute Live Boundary

AutoResearch must never enable:

```text
DRY_RUN=0
LIVE_CANARY_ENABLED=true
GREEN_SNIPER_LIVE_ENABLED=true
RESEARCH_RANK_CANARY_LIVE_ENABLED=true
MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED=true
SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED=true
AUTO_PROMOTE_LIVE=true
MODEL_AUTO_PROMOTE=true
LLM_TRADING_ENABLED=true
```

## Files Added In Base Block

- `research_loop/program.md`
- `research_loop/runbook.md`
- `research_loop/README.md`
- `research_loop/safety.yaml`
- `research_loop/safety.py`
- `strategy_proposals/schema.autoresearch.json`
- `research_loop/experiment_schema.py`
- `research_loop/objectives.yaml`
- `research_loop/objectives.py`

## Validation

Use:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\strategy_quality_gate.py --warn-only
```
