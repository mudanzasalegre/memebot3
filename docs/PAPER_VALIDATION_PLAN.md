# Paper Validation Plan

Target before live: `avg_pnl_pct > 0`, severe losses down at least 50%, `research_rank_canary avg_pnl > 0`, and `green_sniper_pass` either non-negative or shadowed by guardrails.

Reports:

```powershell
python tools/missed_pumps_report.py
python tools/funnel_attribution_report.py
python tools/trade_diagnostics.py
python tools/runner_capture_report.py
python tools/policy_replay.py
```
