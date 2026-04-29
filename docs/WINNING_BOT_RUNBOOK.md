# Winning Bot Runbook

Use `paper_combined_v1` first. It keeps live off, promotes only rank-high research canary, shadows high-risk green sniper setups and records missed pumps with confirmed posterior outcomes.

Start:

```powershell
python scripts/apply_profile.py paper_combined_v1
.\scripts\start_stack.ps1 -IncludeBot
```

Review every 12-24h: `trade_diagnostics`, `policy_replay`, `runner_capture`, `missed_pumps`, provider health and `sniper/status`.

Do not enable live canary unless `policy_replay combined_v1` improves current total PnL and severe losses, and paper has positive average PnL with reduced `ADVERSE_TICK` / `LIQUIDITY_CRUSH`.
