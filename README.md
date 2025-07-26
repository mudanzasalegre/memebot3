# MemeBot 3 🤖🚀
*A Solana meme‑coin sniper with rule‑based filters + optional ML model*

[![License](https://img.shields.io/badge/License-MIT-green.svg)](#license)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

---

## Table of Contents
1. [What it does](#what-it-does)
2. [Quick start](#quick-start)
3. [Environment variables](#environment-variables)
4. [Running](#running)
5. [How it works](#how-it-works)
6. [Retraining & Calibration](#retraining--calibration)
7. [Roadmap](#roadmap)
8. [Contributing](#contributing)
9. [License](#license)

---

## Requirements
* **Python ≥ 3.10** (tested on 3.11)
* A Solana RPC endpoint
* Bitquery / Helius / RugCheck API keys

## What it does
`memebot3` watches the Solana memecoin jungle and:

| Stage            | Action |
|------------------|--------|
| **Discovery**    | Streams **Pump.fun** mints + latest 500 pairs from **DexScreener** |
| **Hard filters** | Liquidity, 24 h volume, *market‑cap*, holders, anti‑dump, black‑listed creators |
| **Soft score**   | Adds RugCheck, dev‑cluster heuristics, socials, insider alerts |
| **ML (optional)**| Gradient‑Boost probability a trade is profitable in 30 min |
| **Trade**        | Buys via Jupiter / Papermode, manages TP/SL + trailing exits |
| **Retrain loop** | Every Sunday 04 UTC retrains if new model & AUC ↑ |

All writes are idempotent: metrics land in a **Parquet feature‑store** + a tiny **SQLite** DB for positions & tokens.

---

## Quick start
```bash
# 0) ensure Python ≥ 3.10
python --version         # should print 3.10.x or 3.11.x
# clone & enter
git clone https://github.com/mudanzasalegre/memebot3.git
cd memebot3

# create env
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# copy sample config → .env
cp .env.sample .env
# edit API keys & thresholds

# smoke‑test (no on‑chain tx)
python -m run_bot --dry-run --log
```
> **Tip:** set `LOG_LEVEL=DEBUG` in `.env` to see every filter decision.

Optional analysis setup:
```bash
pip install -U pandas pyarrow matplotlib seaborn scikit-learn jupyter notebook
```

---

## Environment variables (excerpt)

| Var | Default | Meaning |
|-----|---------|---------|
| `TRADE_AMOUNT_SOL` | `0.1` | Real SOL size per buy (`0` = paper) |
| `MIN_LIQUIDITY_USD` | `10_000` | Hard filter liquidity *(raise if too noisy)* |
| `MIN_VOL_USD_24H` | `15_000` | Hard filter 24 h volume |
| `MIN_MARKET_CAP_USD` | `5_000` | **NEW** hard‑filter lower bound for market‑cap |
| `MAX_MARKET_CAP_USD` | `20_000` | **NEW** upper bound |
| `MAX_QUEUE_SIZE` | `300` | cap for validation queue |
| `MIN_HOLDERS` | `10` | Min holders unless there are swaps |
| `BITQUERY_TOKEN` | — | Blank = free endpoint (low rate) |
| `RUGCHECK_API_KEY` | — | Rug risk API |
| `HELIUS_API_KEY` | — | Dev‑cluster & dev‑sells |

See `.env.sample` for the full list.

---

## Running
```bash
# standard run (on‑chain)
python -m run_bot

# cron behind tmux / systemd
python -m run_bot --log     # hourly files in /logs
```

| Flag | Effect |
|------|--------|
| `--dry-run` | Paper trading (`trader.papertrading`) |
| `--log` | Adds hourly log rotation |

---

## How it works
```
┌────────────┐   PumpFun  DexScreener
│  fetcher   │───┬────────┬─────────┐
└────────────┘ sanitize  sanitize   │
        │                         archive (revival)
┌────────────┐
│ HARD rules │ basic_filters()
└────────────┘
        ↓
┌────────────┐  +RugCheck, socials, trend…
│ Soft score │
└────────────┘
        ↓
ML proba ≥ `AI_TH` ? —— no → discard
        │ yes
        ↓
 BUY via Jupiter / paper
        ↓
 trailing TP / SL loop
```

### Feature‑store
* `data/features/features_YYYYMM.parquet` (append‑only, ~21 cols)  
* Input for retraining & calibration scripts in `ml/`

---

## Retraining & Calibration
```bash
# on demand
python -m ml.retrain --from data/features --model-out ml/model.pkl
```
* **Automatic**: Sundays 04 UTC – retrain with last ~30 d, deploy if AUC improves.
* **Calibration** notebook: `notebooks/calibration.ipynb`.

---

## Roadmap
- [ ] Curve‑buy support (rank ≤ 40)  
- [ ] Live dashboard (FastAPI + React)  
- [ ] Ensemble models (LightGBM + CatBoost)  
- [ ] Webhook alerts (Discord / Telegram)  

---

## Contributing
PRs welcome 🚀 — open an issue or ping **@mudanzasalegre**

```bash
pre-commit install     # black + ruff
pytest -q              # unit tests
```

---

## License
MIT © 2025 [mudanzasalegre](https://github.com/mudanzasalegre)
