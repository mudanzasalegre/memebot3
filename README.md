![MemeBot 3 banner](assets/memebot3img.jpg)

# MemeBot 3 🤖🚀
*A Solana meme‑coin sniper with regime-aware execution, rule-based filters, and an optional ML edge*

[![License](https://img.shields.io/badge/License-MIT-green.svg)](#license)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

---

## ☕ Donate / Support the Project
If **MemeBot 3** saved you from a rug (or pumped your bags 🚀), consider tipping the devs so we can move the bot from paper‑mode to fully‑fledged *on‑chain* trading:

```
ARczPrEWBbYj6EKoWoavYNd7VeN99PuTD49j5QnE5S2K   # SPL SOL
```

*(¡gracias! 💜 — every SOL goes back into cloud, RPC, and coffee)*

---

## What MemeBot 3 Is

MemeBot 3 is an operational Solana meme-coin sniper and research workstation. It discovers fresh tokens, enriches them with route, price, liquidity, market-cap, momentum and risk data, runs them through regime-aware strategy gates, simulates or executes entries, manages exits, records every decision, and exposes the full system through a FastAPI backend and a React control UI.

The current production philosophy is not "buy everything early". The bot separates productive PnL validation from research acquisition:

| Lane | Purpose | Productive? |
| --- | --- | --- |
| `pump_early_pumpswap_profit` | Main PnL lane. Pumpswap only, real liquidity, strict bucket filters, runner exits. | Yes |
| `pump_early_pumpswap_prime` | Internal high-edge tag inside the productive lane. Same size by default. | Yes |
| `pump_early_sniper_research` | Near misses, proxy liquidity, pumpfun/meteora, toxic buckets, dataset acquisition. | No |
| `dex_mature_shadow` | Mature DEX research and scorecard input. | No |
| `revival_shadow` | Revival research and scorecard input. | No |

The UI is now part of the core system. It is not just a dashboard: it shows source truth, strategy health, ML readiness, queue pressure, trade replays, logs, command history, and local process control.

## Important Risk Notice

This project can submit real Solana swaps when `DRY_RUN=0` or the bot is launched with real-mode settings. Meme coins are extremely risky and can lose liquidity instantly. Run paper mode first, verify the UI state, use tiny sizes, and never fund the wallet with money you cannot lose.

The default workflow should be:

1. Start in paper mode.
2. Let the productive lane produce enough closed trades.
3. Inspect `/analytics`, `/ml`, `/runtime/strategy-health`, and trade replay.
4. Only then consider live canary with minimal size.

## System Overview

```text
Fetchers
  DexScreener, Pump.fun, GeckoTerminal, Jupiter, Helius, RugCheck, BirdEye, GMGN
      |
      v
Discovery queue
  basic filters, retry/backoff, snapshot quality, route checks
      |
      v
Feature builder
  parquet feature store + SQLite token context + runtime/research JSONL events
      |
      v
Strategy runtime
  regime classification, profit lane gate, bucket health, shadow recovery, ML shadow state
      |
      v
Execution
  paper trading or Jupiter-backed real swaps
      |
      v
Position monitor
  partial TP, adverse tick, no-pump, liquidity crush, runner protection, final close
      |
      v
Analytics and UI
  FastAPI + React cockpit + deterministic reports + replay reconstruction
```

## Main Components

| Path | Role |
| --- | --- |
| `run_bot.py` | Main async bot loop: discovery, queue, scoring, gate, buy, monitor, sell, retrain, telemetry. |
| `config/config.py` | Central `.env` loader and typed runtime configuration. |
| `analytics/filters.py` | Hard and soft filters, effective thresholds by regime/profile. |
| `analytics/strategy_runtime.py` | Regime state, health, bucket blocks, recovery/demotion, execution mode decisions. |
| `analytics/sizing.py` | Entry size classification and caps. Current default spend is controlled by `TRADE_AMOUNT_SOL` and `MIN_BUY_SOL`. |
| `analytics/exit_policy.py` | Effective exit policy, partial TP, post-partial runners, adverse tick and no-pump exits. |
| `analytics/research_runtime.py` | Research lane, rank scoring, scorecards and threshold candidates. |
| `features/builder.py` | Runtime feature vector construction. |
| `features/store.py` | Monthly parquet feature store writes. |
| `ml/train.py` | Dataset quality checks, feature matrix, model training, validation, threshold tuning. |
| `ml/retrain.py` | Safe retrain wrapper that only keeps a new model if selection improves. |
| `trader/papertrading.py` | Paper buy/sell simulation with route and price sanity checks. |
| `trader/buyer.py` / `trader/seller.py` | Real buy/sell execution paths. |
| `api/` | FastAPI backend for the UI and operational API. |
| `ui/` | React + Vite operator interface. |
| `scripts/` | Stack launchers, smoke tests, reports, backup and restore. |
| `data/` | Runtime state, SQLite, metrics JSON/JSONL and parquet features. Do not commit. |
| `logs/` | Rotated bot/API logs. Do not commit. |

## Requirements

| Requirement | Notes |
| --- | --- |
| Python | 3.10+, tested in this repo with Python 3.12. |
| Node.js + npm | Required for the React UI. |
| Windows PowerShell | The included stack scripts are PowerShell-first. The Python modules are portable, but scripts are Windows oriented. |
| Solana RPC | Helius or another reliable Solana RPC endpoint. |
| Disk | `data/features/*.parquet`, JSONL events and logs grow over time. |

Install Python dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Install UI dependencies:

```powershell
cd ui
npm install
cd ..
```

## Credentials And External Accounts

You do not need every provider to start in paper mode, but the bot works better with reliable data. Copy `.env.example` to `.env` and fill the credentials you actually use.

```powershell
Copy-Item .env.example .env
notepad .env
```

| Variable | Required For | Where To Get It |
| --- | --- | --- |
| `SOL_PUBLIC_KEY` | Wallet display, live mode sanity. | Your Solana wallet public address. |
| `SOL_PRIVATE_KEY` | Real on-chain buys/sells. Not needed for paper. | Export from a dedicated hot wallet only. Never use your main wallet. |
| `SOL_RPC_URL`, `RPC_URL`, `HELIUS_RPC_URL` | Solana RPC, balance, signing, price/route support. | Helius dashboard or another Solana RPC provider. |
| `HELIUS_API_KEY` | Helius REST, cluster/dev signals, richer token context. | Helius dashboard. |
| `BIRDEYE_API_KEY` | BirdEye fallback data if enabled. | BirdEye developer portal. |
| `RUGCHECK_API_KEY` | Rug/risk enrichment. | RugCheck API access. |
| `BITQUERY_TOKEN` | Optional discovery/enrichment flow. | Bitquery account. |
| `BQ_CLIENT_ID`, `BQ_CLIENT_SECRET` | Optional Bitquery OAuth flow. | Bitquery account. |
| `GMGN_API_KEY` | Optional GMGN enrichment. | GMGN access, if available. |
| `JUP_API_KEY` | Optional Jupiter managed order/execute flow if your Jupiter plan requires it. | Jupiter developer portal. |

Keep `.env` private. Commit `.env.example`, not `.env`.

## Runtime Modes

| Mode | How | Meaning |
| --- | --- | --- |
| Paper | `DRY_RUN=1` or `.\scripts\start_bot.ps1` without `-RealMode` | No real swaps. Uses `trader.papertrading`. |
| Real | `DRY_RUN=0` or `.\scripts\start_bot.ps1 -RealMode` | Can submit real swaps with the configured wallet. |
| Shadow | `ML_GATE_MODE=shadow`, strategy lanes in shadow | Records decisions/outcomes without productive PnL buys. |
| Live canary | strategy health and mode allow live with caps | Used for guarded live rollout. |

Default recommended operation is paper:

```env
DRY_RUN=1
ML_GATE_MODE=shadow
TRADE_AMOUNT_SOL=0.1
MIN_BUY_SOL=0.1
PUMP_EARLY_EXECUTION_MODE=live
DEX_MATURE_EXECUTION_MODE=shadow
REVIVAL_EXECUTION_MODE=shadow
```

## Quick Start

Start API + UI only:

```powershell
.\scripts\start_stack.ps1
```

Start API + UI + bot in paper mode:

```powershell
.\scripts\start_stack.ps1 -IncludeBot
```

Start API + UI + bot in real mode:

```powershell
.\scripts\start_stack.ps1 -IncludeBot -BotRealMode
```

URLs:

| Service | URL |
| --- | --- |
| UI | `http://127.0.0.1:5173` |
| API docs | `http://127.0.0.1:8000/docs` |
| API health | `http://127.0.0.1:8000/api/v1/health` |

Default local login:

| User | Password | Role |
| --- | --- | --- |
| `viewer` | `viewer` | Read-only monitoring and saved own views. |
| `operator` | `operator` | Runtime commands: pause/resume, retrain, refresh reports, reload model. |
| `admin` | `admin` | Operator plus start/stop managed bot process and log-level control. |

Change local UI users in `.env`:

```env
UI_AUTH_MODE=local
UI_LOCAL_USERS=viewer:strong-viewer-pass:viewer:Viewer;operator:strong-operator-pass:operator:Operator;admin:strong-admin-pass:admin:Admin
UI_SESSION_SECRET=replace-with-a-long-random-secret
```

Emergency loopback-only mode:

```env
UI_AUTH_MODE=dev
```

## Running Components Manually

API:

```powershell
.\scripts\start_api.ps1
```

UI:

```powershell
.\scripts\start_ui.ps1
```

UI with dependency install:

```powershell
.\scripts\start_ui.ps1 -InstallIfMissing
```

Bot in paper mode:

```powershell
.\scripts\start_bot.ps1
```

Bot in real mode:

```powershell
.\scripts\start_bot.ps1 -RealMode
```

Direct Python bot run:

```powershell
.\.venv\Scripts\python.exe run_bot.py --dry-run --log
```

## The Operator UI

The React UI is an operational cockpit built around three groups.

| Page | Purpose |
| --- | --- |
| Overview | Daily cockpit: runtime posture, queue, wallet, ML, source truth, position summary. |
| Runtime | Heartbeat, pause flags, buy limiter, strategy health, demotion/recovery state. |
| Discovery | Funnel feed showing rejects, waits, shadows, buys and exact reasons. |
| Queue | Pending candidates, retries, backoff, oldest candidate, queue pressure. |
| Positions | Open risk inventory with exposure and replay shortcuts. |
| Trades | Closed ledger with outcome, PnL and replay entry points. |
| Trade Replay | Reconstructs one trade from DB facts, runtime events, research events and nearest feature snapshot. |
| Analytics | Edge by exit, lane, regime, sizing, feature coverage and consistency checks. |
| ML Center | Model existence, training status, thresholds, scorecard, readiness and blockers. |
| Config Center | Effective config and derived policies actually running now. |
| Logs and Events | App log tail plus runtime/research JSONL rails. |
| Control Center | Pause/resume, retrain, reload model, refresh reports, process start/stop and command audit. |

The API is protected by local session cookies. `/api/v1/health` and auth endpoints are public; operational endpoints require login.

## API Surface

All API endpoints use `/api/v1` and return a normalized envelope:

```json
{
  "data": {},
  "meta": {
    "generated_at": "2026-04-25T09:30:00+00:00",
    "degraded": false,
    "empty": false,
    "stale": false,
    "source_status": []
  }
}
```

Key endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/health` | API health. |
| `GET /api/v1/auth/session` | Current UI identity/session. |
| `POST /api/v1/auth/login` | Local login. |
| `GET /api/v1/sources/status` | SQLite, JSONL, parquet and metrics source truth. |
| `GET /api/v1/overview` | Main UI summary. |
| `GET /api/v1/runtime/state` | Runtime snapshot from `bot_runtime_state`. |
| `GET /api/v1/runtime/strategy-health` | Regime, lane, bucket and recovery health. |
| `GET /api/v1/discovery/feed` | Candidate funnel feed. |
| `GET /api/v1/discovery/summary` | Stage/reason aggregates. |
| `GET /api/v1/queue/summary` | Queue counters. |
| `GET /api/v1/queue/items` | Current queue items. |
| `GET /api/v1/positions/open` | Open positions. |
| `GET /api/v1/trades/closed` | Closed trades. |
| `GET /api/v1/trades/{trade_id}` | Trade factsheet. |
| `GET /api/v1/trades/{trade_id}/replay` | Full trade reconstruction. |
| `GET /api/v1/analytics/edge` | Edge report summary. |
| `GET /api/v1/analytics/baseline` | Static baseline/context. |
| `GET /api/v1/config/effective` | Effective loaded config. |
| `GET /api/v1/config/policies` | Derived filter/sizing/exit/strategy policies. |
| `GET /api/v1/ml/status` | Model state, train status, blockers, live usage posture. |
| `GET /api/v1/ml/research` | Research scorecard and thresholds. |
| `GET /api/v1/logs/tail` | Whitelisted log tail. |
| `GET /api/v1/events/runtime` | Runtime JSONL events. |
| `GET /api/v1/events/research` | Research JSONL events. |
| `GET /api/v1/control/state` | Control flags and command state. |
| `GET /api/v1/control/process` | Managed bot process state. |
| `POST /api/v1/control/commands` | Queue runtime command. |
| `POST /api/v1/control/process/start` | Start managed bot process. |
| `POST /api/v1/control/process/stop` | Stop managed bot process. |
| `GET/POST/PATCH/DELETE /api/v1/saved-views` | UI saved views. |

## Strategy Logic

The bot classifies opportunities into regimes and lanes before execution.

| Concept | Meaning |
| --- | --- |
| `entry_regime` | Broad behavioral regime: `pump_early`, `dex_mature`, `revival`. |
| `entry_lane` | Product/research lane, for example `pump_early_pumpswap_profit`. |
| `gate_profile` | Detailed gate tag, for example `pumpswap_profit_prime` or `pumpswap_meteor_prime`. |
| `size_bucket` | Execution/sizing label stored on the position. |
| `rank_score` | Research rank score. In the current profit lane, live uses heuristic/bucket gates, not raw `ml_proba`. |

Current productive gate for `pump_early_pumpswap_profit` is built around:

| Rule | Default |
| --- | --- |
| DEX allowlist | `pumpswap` |
| Real liquidity | required |
| Min liquidity | `5000` USD |
| Min score | `35` |
| Age window | `3` to `30` minutes |
| Max price impact | `10%` |
| Blocked mcap bucket | `25000` to `50000` USD |
| Blocked `price_pct_5m` ranges | `0:25`, `50:100` |
| Shape guard | enabled |
| Max productive open positions in paper | `2` |
| Max productive open positions in live canary | `1` |

Research remains active so the bot keeps learning about rejected candidates, but research outcomes do not contaminate the productive paper ledger.

## Sizing

The default spend is now simple and explicit:

```env
TRADE_AMOUNT_SOL=0.1
MIN_BUY_SOL=0.1
```

In paper mode the bot uses the configured per-trade amount for simulation. In real mode this is the notional target before route, wallet, slippage and execution constraints.

The code still records multipliers and buckets for analysis, but `ml_proba` does not increase size by default:

```env
AI_SIZING_ENABLED=false
```

Do not increase size just because win rate looks good over a small sample. Promote size only after enough closed productive trades, stable median PnL and low severe-exit frequency.

## Exits And Runner Capture

The exit engine is regime-aware and lane-aware. For the productive pumpswap lane, the important exits are:

| Exit | Trigger |
| --- | --- |
| Partial TP | `PUMP_EARLY_TP_PARTIAL_TRIGGER_PCT=4`, selling `PUMP_EARLY_TP_PARTIAL_FRACTION=0.80`. |
| Post-partial protection | Lock floor and giveback protection after partial. |
| Broad runner | Baseline runner profile for normal productive trades. |
| Prime runner | More room after stronger prime entries. |
| Meteor runner | More room for meteor/prime runners with larger peak step-ups. |
| `ADVERSE_TICK` | Productive lane exits if PnL is bad after the configured early seconds. |
| `NO_PUMP_EXIT` | Productive lane exits if no meaningful peak appears in the first minutes. |
| `LIQUIDITY_CRUSH` | Liquidity collapse protection. |
| `EARLY_DROP`, `STOP_LOSS`, `TIME_STOP` | General pre-partial risk controls. |

Core runner settings are visible in `/api/v1/config/policies`.

## Health, Demotion And Recovery

`analytics/strategy_runtime.py` protects the bot from continuing to trade bad conditions. It tracks:

| Signal | Used For |
| --- | --- |
| Average PnL windows | Detect negative expectancy. |
| Consecutive losses | Early demotion. |
| Severe exits | `LIQUIDITY_CRUSH`, `STOP_LOSS`, `EARLY_DROP`, `ADVERSE_TICK`, or close <= `-25%`. |
| Bucket health | Blocks specific toxic lane/dex/mcap/price5m buckets instead of killing all trading. |
| Shadow productive recovery | Lets the productive lane recover when recent rebased shadow outcomes prove the current gate is healthy again. |

The key current recovery settings:

```env
PUMP_EARLY_SHADOW_RECOVERY_ENABLED=true
PUMP_EARLY_SHADOW_RECOVERY_WINDOW=8
PUMP_EARLY_SHADOW_RECOVERY_MIN_TRADES=8
PUMP_EARLY_SHADOW_RECOVERY_MIN_AVG_PNL_PCT=5
PUMP_EARLY_SHADOW_RECOVERY_MIN_WIN_RATE_PCT=45
PUMP_EARLY_SHADOW_RECOVERY_MAX_SEVERE_EXITS=2
PUMP_EARLY_SHADOW_RECOVERY_MAX_LIQ_CRUSH=1
PUMP_EARLY_SHADOW_RECOVERY_MAX_CONSECUTIVE_LOSSES=3
PUMP_EARLY_SHADOW_RECOVERY_MAX_AGE_H=36
```

Check the current state in:

```text
UI -> Runtime -> Strategy Health
GET /api/v1/runtime/strategy-health
```

## Machine Learning

ML is intentionally optional and currently safe by default:

```env
ML_GATE_MODE=shadow
AI_SIZING_ENABLED=false
```

That means the model can train, score and publish status, but it does not block live/paper buys and does not increase size. Productive entries are governed by the profit lane heuristic and bucket gate.

Training uses:

| Source | Use |
| --- | --- |
| `data/features/features_YYYYMM.parquet` | Feature snapshots and metadata. |
| `candidate_outcomes.jsonl` | Research/shadow outcomes. |
| `positions` in SQLite | Closed trade outcomes. |
| `ml/model.pkl` | Active model. |
| `ml/model.meta.json` | Model metadata. |
| `data/metrics/recommended_threshold.json` | Tuned threshold candidate. |
| `data/metrics/train_status.json` | Last training attempt, blockers and counts. |
| `data/metrics/dataset_quality.json` | Dataset quality pass/fail details. |

Default training eligibility is conservative:

```env
ML_MIN_DATASET_ROWS=190
ML_MIN_UNIQUE_TOKENS=190
ML_MIN_POSITIVES=40
ML_MIN_HOLDOUT_ROWS=40
ML_MIN_HOLDOUT_POSITIVES=8
ML_TRAIN_ENTRY_LANE_ALLOWLIST=pump_early_pumpswap_profit,pump_early_pumpswap_prime,pump_early_meteor_prime
ML_TRAIN_DEX_ALLOWLIST=pumpswap
```

Manual retrain:

```powershell
.\.venv\Scripts\python.exe -m ml.retrain
```

ML report:

```powershell
.\.venv\Scripts\python.exe scripts\ml_report.py
```

## Data Layout

| Path | Description |
| --- | --- |
| `data/memebotdatabase.db` | SQLite source of truth for tokens, positions, runtime state, commands and saved views. |
| `data/features/features_YYYYMM.parquet` | Append-only feature store. |
| `data/metrics/runtime_events.jsonl` | Runtime event rail. |
| `data/metrics/candidate_outcomes.jsonl` | Research and candidate outcome rail. |
| `data/metrics/research_scorecard.json` | Research scorecard. |
| `data/metrics/research_thresholds.json` | Threshold/rank candidates. |
| `data/metrics/recommended_threshold.json` | ML threshold candidate. |
| `data/metrics/train_status.json` | Last train status. |
| `data/paper_portfolio.json` | Paper portfolio compatibility artifact. |
| `logs/*.txt` | Hourly application logs when `--log` is enabled. |
| `ml/model.pkl` | Active ML model. |
| `ml/model.meta.json` | Active model metadata. |

Do not commit `data/`, `logs/`, `.env`, wallet keys or local backups.

## Reports

Reports write Markdown into `docs/` by default and also print to stdout.

```powershell
.\.venv\Scripts\python.exe scripts\baseline_report.py
.\.venv\Scripts\python.exe scripts\edge_report.py
.\.venv\Scripts\python.exe scripts\ml_report.py
.\.venv\Scripts\python.exe scripts\pnl_rollout_report.py
```

| Report | Output |
| --- | --- |
| `baseline_report.py` | Config, DB, parquet and feature baseline. |
| `edge_report.py` | Edge by exits, regimes, sizing, requeues and feature coverage. |
| `ml_report.py` | Dataset quality, train status, model meta and validation predictions. |
| `pnl_rollout_report.py` | Deterministic rollout comparison around historical slices and current lanes. |

## Quality Gates

Fast backend/API gate without UI build:

```powershell
.\.venv\Scripts\python.exe scripts\quality_gate.py --skip-ui-build
```

Full local gate including UI build:

```powershell
.\.venv\Scripts\python.exe scripts\quality_gate.py
```

PowerShell wrapper:

```powershell
.\scripts\quality_gate.ps1
```

Targeted tests:

```powershell
python -m pytest tests/test_exit_policy.py tests/test_strategy_runtime.py tests/test_research_runtime.py tests/test_pump_live_floor.py
```

Note: `scripts/quality_gate.py` enforces the project venv at `.\.venv\Scripts\python.exe`.

## Backup And Restore

Create a runtime backup:

```powershell
.\scripts\backup_runtime.ps1
```

Include `.env` only when you explicitly want secrets in the backup:

```powershell
.\scripts\backup_runtime.ps1 --with-env --with-logs
```

Restore:

```powershell
.\scripts\restore_runtime.ps1 .\backups\memebot3-backup-YYYYMMDD-HHMMSS.zip --force
```

Restore `.env` from a backup only if you trust that backup:

```powershell
.\scripts\restore_runtime.ps1 .\backups\memebot3-backup-YYYYMMDD-HHMMSS.zip --force --with-env
```

Restore creates a pre-backup and only restores safe runtime paths.

## Operational Runbook

Daily checks:

1. Open `http://127.0.0.1:5173`.
2. Check `Overview` source truth: SQLite, runtime events, features, paper portfolio, scorecard.
3. Check `Runtime`: heartbeat fresh, buys not paused, strategy health not stuck in cooldown unless justified.
4. Check `Discovery`: top reject reasons. `recovery_not_ready`, `bucket`, `no_liq` and API rate limits tell different stories.
5. Check `Analytics`: closed trades, win rate, median PnL, exits, feature coverage.
6. Check `ML Center`: model loaded, train status, blockers and whether ML is still shadow.
7. Use `Trade Replay` for any large loss or runner to inspect the exact timeline.

If no buys happen for hours:

1. Confirm `buys_paused=false` and `discovery_paused=false`.
2. Inspect `Runtime -> Strategy Health` for `shadow_wait`, `cooldown`, blocked buckets and `last_disable_reason`.
3. Inspect `Discovery -> Summary` for reject reasons.
4. Inspect `/api/v1/ml/status` to confirm ML is not enforcing.
5. Tail latest `logs/*-N.txt` and `data/metrics/candidate_outcomes.jsonl`.

If the model does not train:

1. Open `ML Center`.
2. Check `last_train_status`, `eligible_rows`, `eligible_unique_tokens`, `eligible_positives`, `holdout_rows`, `skip_reasons`, `rows_to_next_model`.
3. Run `.\.venv\Scripts\python.exe scripts\ml_report.py`.
4. Do not force a model into live gating until validation is acceptable.

## Live Trading Checklist

Before live:

1. `DRY_RUN=0` only when intentional.
2. Use a dedicated hot wallet, never your main wallet.
3. Fund only the test amount.
4. Confirm `SOL_PRIVATE_KEY`, `SOL_PUBLIC_KEY`, RPC URLs and Jupiter settings.
5. Confirm `TRADE_AMOUNT_SOL=0.1` and `MIN_BUY_SOL=0.1` or lower if you deliberately reduce exposure.
6. Confirm `ML_GATE_MODE=shadow` unless you have explicitly validated ML.
7. Confirm `PUMP_EARLY_EXECUTION_MODE=live`, `DEX_MATURE_EXECUTION_MODE=shadow`, `REVIVAL_EXECUTION_MODE=shadow`.
8. Confirm UI shows no stale sources.
9. Start with `.\scripts\start_stack.ps1 -IncludeBot -BotRealMode`.
10. Watch the first trades from `Runtime`, `Positions`, `Trades`, `Logs and Events`.

If anything looks wrong, pause buys from `Control Center` or stop the bot process.

## Troubleshooting

| Symptom | Likely Cause | Check |
| --- | --- | --- |
| UI loads but API calls fail | API not running or proxy target wrong. | `http://127.0.0.1:8000/api/v1/health`, `VITE_API_PROXY_TARGET`. |
| Login fails | Wrong local users or cookie state. | `UI_LOCAL_USERS`, clear browser cookies. |
| Bot shows `external` | It was started manually, not by UI process manager. | Stop it in its original console. |
| No buys | Health/cooldown, bucket blocks, strict gate, no route, no liquidity, API rate limits. | Discovery summary, strategy health, logs. |
| Model missing | Dataset quality not ready or retrain skipped. | ML Center, `data/metrics/train_status.json`. |
| `numpy/pyarrow` import errors | Wrong Python interpreter. | Use `.\.venv\Scripts\python.exe`. |
| GeckoTerminal `429` | Rate limiting. | Reduce fallback pressure or wait; check logs. |
| SQLite appears stale | Bot not publishing state or API reading different path. | `SQLITE_DB`, `/sources/status`. |

## GitHub Hygiene

Before pushing:

```text
Commit:
  README.md
  .env.example
  source code
  tests
  docs that are meant to be public

Do not commit:
  .env
  data/
  logs/
  backups with .env
  wallet keys
  private API keys
  local model experiments if you do not want them public
```

Recommended `.gitignore` entries:

```gitignore
.env
.venv/
__pycache__/
.pytest_cache/
data/
logs/
backups/
ml/*.pkl
ml/*.meta.json
ui/node_modules/
ui/dist/
*.bkup.*
```

## Project Documentation

Additional docs:

| File | Purpose |
| --- | --- |
| `docs/API_UI_SPEC.md` | API/UI contract and endpoint details. |
| `docs/UI_OPERATIONS.md` | Local UI/API/bot runbook, backup and restore. |
| `docs/UI_SITEMAP.md` | UI navigation map. |
| `docs/UI_STATE_CONTRACT.md` | UI state expectations and source contracts. |
| `docs/UI_VISUAL_CHARTER.md` | UI visual design notes. |
| `docs/BASELINE.md` | Generated baseline report. |
| `docs/EDGE_REPORT.md` | Generated edge report. |
| `docs/ML_REPORT.md` | Generated ML report. |
| `docs/ROLLOUT_REPORT.md` | Generated rollout report. |

## Contributing

Use small, testable patches. For strategy changes, include a deterministic replay/report whenever possible.

Recommended checks:

```powershell
python -m pytest
.\.venv\Scripts\python.exe scripts\quality_gate.py --skip-ui-build
```

For UI changes:

```powershell
cd ui
npm run build
```

## License

MIT © 2025 [mudanzasalegre](https://github.com/mudanzasalegre)
# ML lane-aware rollout

The bot supports a conservative lane-aware ML policy. Use `ML_GATE_MODE=lane_aware`
with live profit lanes in `sizing_only`; do not enable live `enforce` until
`data/metrics/segment_report.json` and `lane_promotion_status.json` show that
the model improves realized PnL for that lane without losing jackpots.

Useful commands:

```bash
python tools/audit_ml_baseline.py
python -m ml.segment_report
python tools/ml_status.py
python backtest/replay.py --policy lane_aware
```
