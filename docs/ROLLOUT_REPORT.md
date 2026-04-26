# Rollout Report

- DB: `D:\Dev\Python\memebot3\data\memebotdatabase.db`
- Closed slice cutoff: `2026-04-15T04:02:52+00:00` (15 Apr 2026 06:02:52 CEST)

## History

- All closed history: closed=196 avg=1.79% median=-7.07% win=41.33% liq_crush=7.65% loss_streak=9
- Post-cutoff closed history: closed=25 avg=-15.93% median=-14.23% win=16.00% liq_crush=0.00% loss_streak=9
- pump_early only: closed=196 avg=1.79% median=-7.07% win=41.33% liq_crush=7.65% loss_streak=9
- pump_early post-cutoff: closed=25 avg=-15.93% median=-14.23% win=16.00% liq_crush=0.00% loss_streak=9

## Readiness

- Paper readiness: `False` checks={"avg_pnl_pct": false, "closed_trades": true, "liq_crush_rate_pct": false, "median_pnl_pct": false, "win_rate_pct": false}
- Live canary start readiness: `False` first10={"avg_pnl_pct": null, "closed_trades": 0, "liq_crush_count": 0, "liq_crush_rate_pct": null, "max_loss_streak": 0, "median_pnl_pct": null, "win_rate_pct": null}
- Live canary promotion readiness: `False` first25={"avg_pnl_pct": null, "closed_trades": 0, "liq_crush_count": 0, "liq_crush_rate_pct": null, "max_loss_streak": 0, "median_pnl_pct": null, "win_rate_pct": null}

## Research

- Scorecard generated: `2026-04-15T20:09:36.454383+00:00`
- Thresholds generated: `2026-04-15T20:09:36.449364+00:00`
- Scorecard live_closed: `196`
- Threshold regimes: `dex_mature,pump_early`
