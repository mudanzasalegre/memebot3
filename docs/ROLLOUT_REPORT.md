# Rollout Report

- DB: `D:\Dev\Python\memebot3\data\memebotdatabase.db`
- Closed slice cutoff: `2026-04-15T04:02:52+00:00` (15 Apr 2026 06:02:52 CEST)

## History

- All closed history: closed=85 avg=-3.47% median=-16.74% win=28.24% liq_crush=24.71% loss_streak=11
- Post-cutoff closed history: closed=85 avg=-3.47% median=-16.74% win=28.24% liq_crush=24.71% loss_streak=11
- pump_early only: closed=74 avg=-9.81% median=-20.28% win=25.68% liq_crush=28.38% loss_streak=11
- pump_early post-cutoff: closed=74 avg=-9.81% median=-20.28% win=25.68% liq_crush=28.38% loss_streak=11
- pump_early_pumpswap_profit: closed=3 avg=-57.27% median=-68.46% win=0.00% liq_crush=66.67% loss_streak=3
- pump_early_pumpswap_prime: closed=3 avg=-57.27% median=-68.46% win=0.00% liq_crush=66.67% loss_streak=3

## Readiness

- Paper readiness: `False` checks={"avg_pnl_pct": false, "closed_trades": false, "liq_crush_rate_pct": false, "median_pnl_pct": false, "win_rate_pct": false}
- Live canary start readiness: `False` first10={"avg_pnl_pct": null, "closed_trades": 0, "liq_crush_count": 0, "liq_crush_rate_pct": null, "max_loss_streak": 0, "median_pnl_pct": null, "win_rate_pct": null}
- Live canary promotion readiness: `False` first25={"avg_pnl_pct": null, "closed_trades": 0, "liq_crush_count": 0, "liq_crush_rate_pct": null, "max_loss_streak": 0, "median_pnl_pct": null, "win_rate_pct": null}

## Gate Replay

- Pump candidates with late/reject context: `16424`
- Current conservative gate pass: `2`
- Sniper core pass: `270`
- Sniper micro-momentum pass: `383`
- Sniper any pass: `550`
- Pumpswap profit raw pass: `353`
- Pumpswap profit pass: `83`
- Pumpswap prime pass: `83`
- Pumpswap meteor-prime pass: `0`

## Research

- Scorecard generated: `2026-05-13T21:06:57.843956+00:00`
- Thresholds generated: `2026-05-13T21:06:57.843956+00:00`
- Scorecard live_closed: `84`
- Threshold regimes: `dex_mature,pump_early`
