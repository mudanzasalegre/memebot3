# AutoResearch Program

## Goal

Maximize `objective_score` safely by testing candidate policy/config changes in
local replay first and controlled paper-forward later.

## Inspiration

`karpathy/autoresearch` keeps or discards edits by running a fixed experiment and
measuring a target validation metric. MemeBot3 uses the same keep/discard idea,
but the mutable artifact is a candidate policy/profile, not the trading runtime.

```text
karpathy/autoresearch:
  edit train.py
  run fixed experiment
  measure val_bpb
  keep/discard

MemeBot3 AutoResearch:
  generate candidate_policy/profile
  validate safety
  run local replay
  measure objective_score
  accept/reject
```

## Mutable Surface

AutoResearch may create:

- `candidate_policy.json`
- sandbox `candidate.env`
- replay/paper reports under `data/research_runs/`
- disabled live export artifacts for manual future review

It may adjust only paper/replay research settings such as thresholds, lane
sizing, and exit parameters within the safety caps.

## Forbidden Surface

AutoResearch must never:

- Activate live trading.
- Set `DRY_RUN=0` or `DRY_RUN=false`.
- Enable live canary flags.
- Enable live auto-promotion.
- Touch buyer, seller, wallet, signer, private keys, or RPC secrets.
- Edit `run_bot.py` as the experimental surface.
- Disable risk guards.
- Turn social signals into hard blocking gates.
- Increase API rate limits or discovery frequency.
- Call external APIs during replay.

## Replay Loop

Replay is local-only:

```text
reports -> candidate_policy -> safety -> sandbox -> replay -> metrics
```

Replay must read existing local data and reports. It must not fetch new market,
RPC, social, or provider data.

## Paper-Forward Loop

Paper-forward is allowed only after replay acceptance and only through a
paper-only profile. It must respect existing provider rate limits and normal bot
cooldowns. It must not enable live execution.

## API Budget

AutoResearch is API-budget aware:

- Candidate generation does not call external APIs.
- Objective scoring does not call external APIs.
- Replay does not call external APIs.
- Paper-forward uses the normal bot provider limits.
- Candidates that touch protected provider cadence/RPM keys are rejected.
- Candidates that increase 429/cooldown/provider degradation metrics are
  rejected by objective gates when those metrics are available.

## Safety Gates

Every candidate must pass `research_loop/safety.yaml` and
`validate_candidate_safety()` before sandboxing or replay. The safety contract
blocks live flags, secrets, provider rate limit changes, and paper sizing above
caps.

## Scoring

`research_loop/objectives.py` computes metric deltas against a baseline and then
applies:

- Positive weighted improvements.
- Risk/API/overtrading penalties.
- Hard gates for PnL, severe losses, liquidity crush, adverse ticks, API 429s,
  runner capture, median PnL, and drawdown.

## Keep/Discard Logic

```text
if safety fails:
  reject
elif hard gates fail:
  reject
elif objective_score <= 0:
  reject
else:
  accept replay candidate
```

Accepted replay candidates can later become paper-forward candidates. No live
promotion is automatic.

## LLM Contract

An LLM is not a trader. If enabled in the future, it may only propose candidate
policies under schema and safety validation. It may not edit runtime trading
code, touch live settings, call external APIs, or bypass the evaluator.

Default flags:

```env
AUTORESEARCH_LLM_ENABLED=false
AUTORESEARCH_LLM_CAN_EDIT_CODE=false
AUTORESEARCH_LLM_CAN_TOUCH_LIVE=false
AUTORESEARCH_LLM_CAN_CALL_APIS=false
```

LLM program:

```text
You are an autonomous research agent.
You optimize objective_score.
You may generate candidate policies.
You may not edit trading runtime.
You may not touch live.
You may not call external APIs.
You must respect safety.yaml.
You must log every experiment.
You must keep/discard based on evaluator.
```

`research_loop/llm_adapter.py` is disabled by default. When disabled it is a
no-op. If a future operator enables it, the adapter still rejects unsafe
capability flags and only accepts generated `candidate_policy.json` payloads
that pass the AutoResearch schema and safety contract.
