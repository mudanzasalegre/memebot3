# MemeBot3 AutoResearch

AutoResearch is the local research loop for MemeBot3. It adapts the experiment
pattern from `karpathy/autoresearch` to trading configuration research without
allowing the agent to edit trading runtime code or activate live execution.

The loop is:

```text
local reports
-> candidate_policy
-> safety validation
-> sandbox profile
-> replay
-> objective_score
-> accept/reject
-> paper-forward
-> scoreboard
```

This first block implements the base contract only:

- Program documentation.
- Safety rules for candidate policies.
- Candidate policy schema validation.
- Objective scoring and hard gates.

Not implemented in this block:

- Replay runner.
- Candidate generator.
- Paper-forward controller.
- Scheduler.
- LLM adapter.
- UI or API endpoints.

## Mutable Surface

AutoResearch may only generate and evaluate candidate policy/config changes for
paper or replay experiments. The real `.env`, live profiles, wallet, buyer,
seller, RPC secrets, and `run_bot.py` are not mutable experiment surfaces.

## Safety Invariant

The default state must stay paper-only:

```text
DRY_RUN=1
STRATEGY_OPTIMIZATION_LOCK=true
LIVE_CANARY_ENABLED=false
AUTO_PROMOTE_LIVE=false
MODEL_AUTO_PROMOTE=false
LLM_TRADING_ENABLED=false
```

Candidate policies are rejected when they touch forbidden live/secrets/API
budget keys, request live promotion, or exceed paper sizing caps.
