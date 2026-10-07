# Hotfix Runbook

PR-00 freezes the bot in a paper-safe profile after the 2026-07-07 audit:

- net closed PnL: `-81.33 USD`
- profit factor: `0.629`
- `LIQUIDITY_CRUSH`: `-138.99 USD`
- `NO_PUMP_EXIT`: `-55.26 USD`
- no-partial trades: `-215.67 USD`

Do not enable live while this profile is active.

## Paper Startup

Use the PR-00 profile:

```powershell
$env:CONFIG_PROFILE="paper_hotfix_0707"
.\.venv\Scripts\python.exe tools\preflight.py
.\.venv\Scripts\python.exe run_bot.py --dry-run --log
```

The launchers default to `paper_hotfix_0707` when no `CONFIG_PROFILE` or
`CONFIG_PROFILE_PATH` is set:

```powershell
.\scripts\start_bot.ps1
.\scripts\start_stack.ps1 -IncludeBot
```

## Safety Invariants

- `DRY_RUN=1`
- `STRATEGY_OPTIMIZATION_LOCK=true`
- `LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED=false`
- Paper micro caps are finite; `0` is rejected because runtime treats it as unlimited.
- Live, canary, model promotion, AutoResearch live promotion, and LLM trading flags remain false.
- The profile contains no wallet keys, private keys, RPC secrets, API tokens, or passwords.

## Validation

```powershell
$env:CONFIG_PROFILE="paper_hotfix_0707"
.\.venv\Scripts\python.exe -m pytest -q tests\test_preflight_paper_hotfix.py tests\test_start_stack_autoresearch.py
.\.venv\Scripts\python.exe tools\preflight.py
.\.venv\Scripts\python.exe scripts\strategy_quality_gate.py --warn-only
```

`tools/preflight.py` writes `data/metrics/preflight_status.json`. The status
includes a redacted copy of the PR-00 profile validation.

## Expected Boot Banner

At bot startup, `run_bot.py` emits a `PR-00 PAPER-SAFE FREEZE` warning with the
2026-07-07 loss metrics and the active config profile. This is intentional and
should remain visible until later PRs repair ledger, sizing, caps, and risk
guards.
