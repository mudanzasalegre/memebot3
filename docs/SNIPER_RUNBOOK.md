# Sniper Runbook

## Paper sniper

1. Apply the paper profile:

```powershell
.\scripts\apply_profile.ps1 sniper_paper
```

2. Start the stack:

```powershell
.\scripts\start_stack.ps1 -IncludeBot
```

3. Watch `/api/v1/sniper/status`, `data/metrics/sniper_audit.json`, and `docs/MISSED_PUMPS_REPORT.md`.

## What should happen

- PumpPortal/Pumpfun candidates enter `hot_queue`.
- `pump_early_green_candle_sniper` evaluates before conservative pumpswap gates.
- Paper can simulate without route when proxy is allowed.
- Live canary remains off unless the live profile is applied.

## Stop conditions

- Missed pumps report shows repeated `no_route` or `max_open` for >100% pumps.
- Severe exits rise above 10% in green sniper paper.
- Live canary records daily loss or liquidity crush.
