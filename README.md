
![MemeBot 3 banner](assets/memebot3img.jpg)

# MemeBot 3 🤖🚀
*A Solana meme‑coin sniper with rule‑based filters **and** an optional ML edge*

[![License](https://img.shields.io/badge/License-MIT-green.svg)](#license)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

---

## ☕ Donate / Support the Project
If **MemeBot 3** saved you from a rug (or pumped your bags 🚀), consider tipping the devs so we can move the bot from paper‑mode to fully‑fledged *on‑chain* trading:

```
ARczPrEWBbYj6EKoWoavYNd7VeN99PuTD49j5QnE5S2K   # SPL SOL
```

*(¡gracias! 💜 — every SOL goes back into cloud, RPC, and coffee)*

---

## Table of Contents
1. [What it does](#what-it-does)
2. [Quick start](#quick-start)
3. [Environment variables](#environment-variables)
4. [Running](#running)
5. [How it works](#how-it-works)
6. [Retraining & Calibration](#retraining--calibration)
7. [Roadmap](#roadmap)
8. [Contributing](#contributing)
9. [License](#license)

---

## Requirements
* **Python ≥ 3.10** (tested on 3.11)
* A Solana RPC endpoint
* API keys: Bitquery · Helius · RugCheck *(optional: BirdEye)*

---

## What it does
`memebot3` monitors the Solana memecoin jungle and:

| Stage | Action |
|-------|--------|
| **Discovery** | Streams **Pump.fun** mints + latest 500 pairs from **DexScreener** |
| **Hard filters** | Liquidity, 24 h volume, *market‑cap*, holders, anti‑dump, black‑listed creators |
| **Soft score** | Adds RugCheck, dev‑cluster heuristics, socials, insider alerts |
| **ML (optional)** | Gradient‑Boost probability a trade is profitable in ≤ 30 min |
| **Trade** | Buys via Jupiter *(or paper‑mode)*, manages TP/SL + trailing exits |
| **Retrain loop** | Every Sunday 04 UTC retrains if new model & AUC ↑ |

Writes are idempotent: metrics land in a **Parquet feature‑store** and a tiny **SQLite** DB for positions & tokens.

---

## Quick start
```bash
# 0) ensure Python ≥ 3.10
python --version          # should print 3.10.x or 3.11.x

# clone & enter
git clone https://github.com/mudanzasalegre/memebot3.git
cd memebot3

# create env
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# copy sample config → .env
cp .env.sample .env
# edit API keys & thresholds

# smoke‑test (no on‑chain tx)
python -m run_bot --dry-run --log
```
> **Tip:** set `LOG_LEVEL=DEBUG` in `.env` to see every filter decision.

Optional data‑science extras:
```bash
pip install -U pandas pyarrow matplotlib scikit-learn jupyter notebook
```

---

## Environment variables (excerpt)

| Var | Default | Meaning |
|-----|---------|---------|
| `TRADE_AMOUNT_SOL` | `0.15` | Real SOL size per buy (`0` = paper) |
| `MIN_LIQUIDITY_USD` | `3 500` | Hard filter liquidity *(raise if too noisy)* |
| `MIN_VOL_USD_24H` | `7 500` | Hard filter 24 h volume |
| `MIN_MARKET_CAP_USD` | `3 000` | Lower bound for market‑cap |
| `MAX_MARKET_CAP_USD` | `400 000` | Upper bound |
| `MIN_AGE_MIN` | `8` | Ignore tokens younger than **8 min** |
| `MAX_QUEUE_SIZE` | `300` | Cap for validation queue |
| `BIRDEYE_API_KEY` | — | Enables BirdEye fallback *(60 RPM free)* |
| `GECKO_API_URL` | <https://api.geckoterminal.com/api/v2> | GeckoTerminal fallback |
| `BITQUERY_TOKEN` | — | Blank = free endpoint (low rate) |
| `RUGCHECK_API_KEY` | — | Rug risk API |
| `HELIUS_API_KEY` | — | Dev‑cluster & dev‑sells |

See `.env.sample` for the full list and sensible defaults.

---

## Running
```bash
# standard run (on‑chain)
python -m run_bot

# tmux / systemd + hourly log rotation
python -m run_bot --log
```

| Flag | Effect |
|------|--------|
| `--dry-run` | Paper trading (`trader.papertrading`) |
| `--log` | Adds hourly log rotation to `/logs` |

---

## How it works
```
┌────────────┐   PumpFun   DexScreener
│  fetcher   │───┬─────────┬──────────┐
└────────────┘  sanitize   sanitize   │
        │                          archive (revival)
┌────────────┐
│ Hard rules │  basic_filters()
└────────────┘
        ↓
┌────────────┐  +RugCheck, socials, trend…
│ Soft score │
└────────────┘
        ↓
 ML proba ≥ AI_TH ?  —— no → discard
        │ yes
        ↓
 BUY via Jupiter / Paper
        ↓
 trailing TP / SL / Trailing‑stop
```

### Feature‑store
* `data/features/features_YYYYMM.parquet` (append‑only, ~21 cols)  
* Input to retraining & calibration scripts in `ml/`

---

## Retraining & Calibration
```bash
# on‑demand
python -m ml.retrain --from data/features --model-out ml/model.pkl
```
* **Automatic**: Sundays 04 UTC – retrain with last 30 d, auto‑deploy if AUC improves.
* **Calibration** notebook: `notebooks/calibration.ipynb`.

---

## Roadmap
- [ ] Curve‑buy support (rank ≤ 40)  
- [ ] Live dashboard (FastAPI + React)  
- [ ] Ensemble models (LightGBM + CatBoost)  
- [ ] Webhook alerts (Discord / Telegram)  

---

## Contributing
PRs welcome 🚀 — open an issue or ping **@mudanzasalegre**

```bash
pre-commit install     # black + ruff
pytest -q              # unit tests
```

---

## License
MIT © 2025 [mudanzasalegre](https://github.com/mudanzasalegre)
