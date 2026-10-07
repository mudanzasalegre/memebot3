# Live Promotion Protocol

Live promotion is manual-only. The default posture remains `DRY_RUN=1`; no live flag, wallet, RPC secret, or auto-promotion path is enabled by this protocol.

## Why Live Is Blocked

The 2026-07-07 audit is not live-safe:

- Net closed PnL: `-81.33 USD`
- Profit factor: `0.629`
- `LIQUIDITY_CRUSH`: `-138.99 USD`
- `NO_PUMP_EXIT`: `-55.26 USD`
- No partial trades: `-215.67 USD`
- Partial trades: `134.35 USD`

Until paper evidence improves, live canary start must remain blocked.

## Promotion Ladder

1. Paper acquisition
   - Keep `DRY_RUN=1`.
   - No live lanes enabled.
   - Collect at least `LIVE_PROMOTION_MIN_PAPER_CLOSED_TRADES` closed trades.

2. Paper validation
   - `current_run_summary.json` must show positive `total_pnl_usd`.
   - `profit_factor` must be at least `LIVE_PROMOTION_MIN_PROFIT_FACTOR`.
   - AutoResearch must have an `accepted_replay` or `accepted_paper` candidate with positive `objective_score`.

3. Manual live canary
   - The operator must request live start explicitly with `confirm_live=true`.
   - The generated runtime profile must include `LIVE_CANARY_PROTOCOL=manual_canary_v1`.
   - The generated profile must include `LIVE_CANARY_MANUAL_APPROVAL=true`, `LIVE_CANARY_APPROVED_BY`, `LIVE_CANARY_APPROVED_AT_UTC`, and `LIVE_CANARY_APPROVAL_ID`.
   - Approval expires after 24 hours.

4. Rollback
   - Stop live canary immediately on liquidity crush, provider critical state, daily loss cap, or manual operator stop.
   - Keep `AUTO_PROMOTE_LIVE=false`, `MODEL_AUTO_PROMOTE=false`, and `ML_AUTO_PROMOTE_LANES=false`.

## Canary Caps

The runtime live profile must use finite caps:

| Setting | Required |
| --- | --- |
| `LIVE_CANARY_MAX_OPEN` | `1` |
| `LIVE_CANARY_MAX_DAILY_BUYS` | `1..3` |
| `LIVE_CANARY_DAILY_LOSS_CAP_SOL` | `>0` and `<=0.05` |
| `LIVE_CANARY_SIZE_SOL` | `>0` and `<=0.01` |
| `GREEN_SNIPER_LIVE_MAX_OPEN` | `1` when green live is enabled |
| `GREEN_SNIPER_LIVE_MAX_DAILY_BUYS` | `1..3` when green live is enabled |
| `GREEN_SNIPER_LIVE_SIZE_SOL` | `>0` and `<=0.01` |

`0` is rejected for live canary caps because this codebase treats `0` as unlimited in several runtime paths.

## Validation Commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_pr16_live_canary_guard.py tests\test_live_promotion_preflight.py tests\test_live_canary.py tests\test_preflight_paper_hotfix.py
.\.venv\Scripts\python.exe tools\preflight.py --run-tests
```

The UI endpoint `GET /api/v1/control/live-preflight` reports the promotion gates. A live process start is only allowed by `POST /api/v1/control/process/start` when `dry_run=false`, `confirm_live=true`, the profile gates pass, sample gates pass, and a named authenticated operator is recorded.
