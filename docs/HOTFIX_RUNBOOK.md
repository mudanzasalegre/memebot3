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
- Runner partials persist ladder state and Bird TP1 is global for normal paper lanes before retrace exits.
- Dynamic runner floor protects remaining runners after partials.
- `research_rank_canary_priority` may bypass legacy shape/profit blockers only with route, real liquidity, rank >=70, txns5m >=1000, liquidity >=15000, and price5m 50..120.
- Momentum ignition may ignore missing trend/second tick only when a strong signal is present; cluster and toxic sell pressure remain hard shadows.
- `pump_early_moonshot_micro_lottery` is paper-only, capped at `0.002 SOL`, max open 1, max daily buys 3, and live must remain false.
- Paper exploration quota is micro-only and cannot override toxic pressure, bad cluster, missing price, or no-route except moonshot paper-only.
- Turbo monitoring is paper-only and best-effort.
- Core reports regenerate at startup, on interval, and after close-count milestones.

New reports:

- `partial_ladder_execution_audit.json`
- `research_rank_priority_report.json`
- `momentum_ignition_fallback_report.json`
- `moonshot_micro_lottery_report.json`
- `current_run_summary.json`
- `entry_funnel_blocker_samples.json`
- `paper_exploration_quota_report.json`
