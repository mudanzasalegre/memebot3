# Sniper Debugging

Use these commands when the bot is not buying:

```powershell
python tools\sniper_audit.py
python tools\missed_pumps_report.py
python tools\backtest_green_sniper.py
```

Primary blockers to inspect:

- `no_route`
- `too_young`
- `too_late_momentum`
- `low_txns_5m`
- `max_open`
- `strategy cooldown`
- `buy_rate_limit`

If a token later moves more than +100% and was not bought, it must appear in `docs/MISSED_PUMPS_REPORT.md`.
