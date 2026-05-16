# Hotfix Runbook

Use the paper profile:

```powershell
$env:CONFIG_PROFILE="paper_hotfix_runner_v2"
.\.venv\Scripts\python.exe run_bot.py
```

Validation:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tools\regenerate_core_reports.py
.\.venv\Scripts\python.exe scripts\strategy_quality_gate.py --warn-only
.\.venv\Scripts\python.exe tools\hotfix_smoke.py
```

Safety invariants:

- `DRY_RUN=1`
- `STRATEGY_OPTIMIZATION_LOCK=true`
- Live canary and green live flags remain false.
- No wallet, buyer, or seller changes are required.
- AutoResearch and model auto-promotion remain disabled.

Operational expectations:

- Untagged or legacy buys are sent to shadow with `untagged_buy_blocked`.
- Sniper research buys require one of the two subprofiles.
- Runner partials persist ladder state and can catch up multiple TP steps in one tick.
- Dynamic runner floor protects remaining runners after partials.
- Turbo monitoring is paper-only and best-effort.
- Core reports regenerate at startup, on interval, and after close-count milestones.
