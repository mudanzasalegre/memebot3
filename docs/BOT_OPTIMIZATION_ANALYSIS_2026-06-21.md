# Bot Optimization Analysis - 2026-06-21

## Data Reviewed

- `data/metrics/runtime_events.jsonl`: 601,071 parsed runtime events, 197.5 MB.
- `data/metrics/candidate_outcomes.jsonl`: 29,913 outcome rows, 17,328 unique addresses.
- `data/memebotdatabase.db`: 21 positions, all closed; 20 at 0.1 SOL and 1 legacy 0.003 SOL.
- `data/research_runs/autoresearch_runtime_state.json`: 41 completed cycles, last cycle at 2026-06-21T10:05:34Z.
- `data/research_runs/runs/ar_late_momentum_s1518194470_0002_75f8a6e2b4`: latest failed replay before fixes.

## Main Findings

1. The active buy rate limiter was not the reason for the pauses: `BUY_RATE_LIMIT_N=0`, `BUY_RATE_LIMIT_WINDOW_S=0`, no trading/block-hour windows were configured, and runtime state had `buys_paused=0`.
2. A non-quota stopper was active: `PAPER_BOOTSTRAP_REQUIRE_COLD_START=true` produced 5,136 `paper_bootstrap_cold_start_complete` blocks in runtime events. This stopped a productive paper path once the model/run was no longer cold.
3. Moonshot was missing extreme winners because every moonshot candidate was shadowed by `cluster_bad`. Candidate outcomes showed 55 unique moonshot rows with peak >=500% and 28 with peak >=1000% that match the new extreme-cluster override.
4. AutoResearch candidates were failing because replay regeneration timed out at 180 seconds. Full core report regeneration took ~222s. Restricting AutoResearch replay regeneration to its actual `REPLAY_REPORTS` set brought it to ~123-130s, and a real failed run now replays as `completed`.
5. Policy Center was slow because backend polling rebuilt heavy reports and parsed huge JSONL files. Before fixes: safety ~64.9s, replay ~8.7s, funnel ~9.8s, ledger ~10.3s. After fixes: safety ~0.06s, replay ~0.004s, funnel ~0.001-0.08s, ledger ~0.15-0.20s.

## Implemented Changes

- Paper/live sizing:
  - Paper trades use 0.1 SOL with `PAPER_MAX_TRADE_AMOUNT_SOL=0.1`.
  - Paper exposure is capped by open invested SOL with `PAPER_MAX_INVESTED_SOL=3.0`.
  - Live fixed sizing uses the wallet-limited computed input, capped at `MAX_TRADE_AMOUNT_SOL=0.1`.
- Removed artificial quota behavior:
  - Kept all open/daily/hourly caps at `0` where `0` means unlimited.
  - Disabled bootstrap cold-start stopping with `PAPER_BOOTSTRAP_REQUIRE_COLD_START=false`.
  - Prevented AutoResearch safety from reintroducing `PAPER_BOOTSTRAP_REQUIRE_COLD_START=true`.
- Moonshot/sniper:
  - Enabled cluster-tail buys.
  - Added `MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_BUY_ENABLED=true`.
  - Extreme cluster override requires `cluster_bad`, non-toxic row, `price_pct_5m >= 500`, `txns_last_5m >= 80`, age <= 10 minutes, and valid market cap.
- AutoResearch:
  - Added targeted `report_names` support to core report regeneration.
  - AutoResearch candidate replay regenerates only the reports it snapshots/evaluates.
  - `start_autoresearch.ps1` no longer passes `--regenerate-reports` by default every cycle.
- Policy UI/backend:
  - Added efficient JSONL tail loading.
  - Avoided full JSONL parsing for source status on large files.
  - Policy replay/funnel endpoints now use cached artifacts when available.
  - Drift now evaluates a bounded tail window by default.
  - Safety endpoint no longer rebuilds config-effect audit on every poll.

## Validation

- Full test suite: `580 passed in 23.02s`.
- AutoResearch replay verification: latest failed run `ar_late_momentum_s1518194470_0002_75f8a6e2b4` now returns `status=completed`, no failures.
- Regenerated AutoResearch replay reports: 17 selected reports, ~130s, no warnings.
- Regenerated key reports confirm:
  - `paper_bootstrap_report.json`: `require_cold_start=false`, `max_open=0`, `max_daily_buys=0`, `max_hourly_buys=0`.
  - `moonshot_micro_lottery_report.json`: cluster-tail and extreme-cluster buys enabled, 55 extreme-cluster candidates identified.
  - `lane_sizing_report.json`: fixed paper trade amount 0.1, no warning truncation.
