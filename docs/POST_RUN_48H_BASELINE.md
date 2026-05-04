# Post-run 48h Baseline

- Generated at UTC: `2026-05-03T16:37:46.760756+00:00`
- Source: `sqlite`
- Raw closed trades: `64`
- Included closed trades: `63`
- Excluded baseline keys: `db:1`

## Global

- Closed trades: `63`
- Win rate: `31.75%`
- Avg PnL: `27.70%`
- Median PnL: `-13.56%`
- Total PnL USD: `145.924076`
- Severe losses: `22`
- Runners >=50/>=100/>=300/>=500: `17/9/3/3`

## By Entry Lane

| Bucket | Count | Win rate | Avg PnL | Median PnL | Severe | Runners >=100 |
|---|---:|---:|---:|---:|---:|---:|
| null | 7 | 14.29% | -23.27% | -33.60% | 4 | 0 |
| pump_early_green_candle_sniper | 13 | 7.69% | -41.52% | -57.74% | 9 | 0 |
| pump_early_sniper_research | 43 | 41.86% | 56.92% | -6.75% | 9 | 9 |

## By Research Sublane

| Bucket | Count | Win rate | Avg PnL | Median PnL | Severe | Runners >=100 |
|---|---:|---:|---:|---:|---:|---:|
| null | 7 | 14.29% | -23.27% | -33.60% | 4 | 0 |
| pump_early_green_candle_sniper | 13 | 7.69% | -41.52% | -57.74% | 9 | 0 |
| pump_early_late_momentum_watch | 4 | 0.00% | -31.79% | -16.16% | 1 | 0 |
| pump_early_research_rank_canary | 39 | 46.15% | 66.02% | -3.14% | 8 | 9 |

## By Exit Reason

| Bucket | Count | Win rate | Avg PnL | Median PnL | Severe | Runners >=100 |
|---|---:|---:|---:|---:|---:|---:|
| ADVERSE_TICK | 16 | 0.00% | -24.55% | -21.06% | 5 | 0 |
| EARLY_DUMP_CUT | 6 | 0.00% | -39.26% | -43.23% | 4 | 0 |
| LIQUIDITY_CRUSH | 8 | 0.00% | -77.81% | -77.26% | 8 | 0 |
| NO_PUMP_EXIT | 8 | 0.00% | -16.46% | -3.84% | 3 | 0 |
| POST_PARTIAL_STOP | 3 | 33.33% | -1.72% | -9.29% | 0 | 0 |
| POST_PARTIAL_TRAILING | 19 | 94.74% | 163.80% | 59.93% | 1 | 8 |
| STOP_LOSS | 2 | 0.00% | -30.64% | -30.64% | 1 | 0 |
| TIMEOUT_HARD | 1 | 100.00% | 81.60% | 81.60% | 0 | 1 |

## By Price5m Bucket

| Bucket | Count | Win rate | Avg PnL | Median PnL | Severe | Runners >=100 |
|---|---:|---:|---:|---:|---:|---:|
| price5m_0_25 | 14 | 28.57% | -9.09% | -18.27% | 6 | 1 |
| price5m_100_180 | 5 | 20.00% | -20.18% | -49.29% | 3 | 0 |
| price5m_180_300 | 1 | 0.00% | -95.74% | -95.74% | 1 | 0 |
| price5m_25_50 | 10 | 20.00% | 158.44% | -17.30% | 3 | 2 |
| price5m_300+ | 4 | 0.00% | -31.79% | -16.16% | 1 | 0 |
| price5m_50_100 | 10 | 40.00% | 29.08% | -15.34% | 4 | 1 |
| price5m_<0 | 19 | 47.37% | 16.88% | -1.95% | 4 | 5 |

## By Mcap Bucket

| Bucket | Count | Win rate | Avg PnL | Median PnL | Severe | Runners >=100 |
|---|---:|---:|---:|---:|---:|---:|
| mcap_100k+ | 10 | 20.00% | -4.54% | -14.00% | 2 | 1 |
| mcap_10k_25k | 8 | 0.00% | -35.12% | -33.15% | 4 | 0 |
| mcap_25k_50k | 25 | 40.00% | 53.78% | -13.56% | 10 | 5 |
| mcap_50k_100k | 14 | 50.00% | 64.20% | 2.20% | 3 | 3 |
| mcap_<10k | 6 | 16.67% | -28.67% | -29.84% | 3 | 0 |

## By Rank Bucket

| Bucket | Count | Win rate | Avg PnL | Median PnL | Severe | Runners >=100 |
|---|---:|---:|---:|---:|---:|---:|
| rank_35_50 | 2 | 0.00% | -10.24% | -10.24% | 0 | 0 |
| rank_50_61 | 19 | 10.53% | -37.05% | -33.60% | 11 | 0 |
| rank_61_75 | 42 | 42.86% | 58.79% | -7.76% | 11 | 9 |

## By Liquidity Bucket

| Bucket | Count | Win rate | Avg PnL | Median PnL | Severe | Runners >=100 |
|---|---:|---:|---:|---:|---:|---:|
| liquidity_10k_25k | 41 | 41.46% | 53.22% | -10.89% | 14 | 8 |
| liquidity_25k+ | 11 | 18.18% | -10.76% | -15.45% | 3 | 1 |
| liquidity_2k_5k | 9 | 0.00% | -32.77% | -23.03% | 4 | 0 |
| liquidity_5k_10k | 2 | 50.00% | -11.90% | -11.90% | 1 | 0 |

## By Liquidity Proxy

| Bucket | Count | Win rate | Avg PnL | Median PnL | Severe | Runners >=100 |
|---|---:|---:|---:|---:|---:|---:|
| real | 63 | 31.75% | 27.70% | -13.56% | 22 | 9 |

## Severe Losses

| Trade | Symbol | Lane | Sublane | Exit | PnL | Peak | Mcap | Price5m | Rank | Liquidity |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| 2 | Eve | null | null | NO_PUMP_EXIT | -43.27% | 0.66% | 11251.000 | 18.770 | 58.713 | 8469.300 |
| 3 | LASER | null | null | STOP_LOSS | -50.39% | 6.87% | 24899.000 | -41.190 | 65.481 | 12533.450 |
| 4 | Lulufee | null | null | NO_PUMP_EXIT | -33.60% | 0.00% | 25765.000 | -7.340 | 56.887 | 12388.680 |
| 5 | Eve | null | null | NO_PUMP_EXIT | -42.22% | 1.80% | 158091.000 | -8.960 | 71.214 | 32968.060 |
| 11 | TROLUNCJAK | pump_early_sniper_research | pump_early_research_rank_canary | ADVERSE_TICK | -44.79% | 0.00% | 157467.000 | 24.230 | 69.233 | 31534.330 |
| 14 | SL | pump_early_sniper_research | pump_early_research_rank_canary | LIQUIDITY_CRUSH | -82.31% | 0.00% | 28111.000 | 15.430 | 65.106 | 13211.200 |
| 19 | techno | pump_early_sniper_research | pump_early_research_rank_canary | LIQUIDITY_CRUSH | -81.19% | 0.00% | 36310.000 | 15.950 | 62.550 | 14763.480 |
| 23 | Guido | pump_early_sniper_research | pump_early_research_rank_canary | ADVERSE_TICK | -28.57% | 0.00% | 53603.000 | 22.870 | 58.150 | 17947.140 |
| 24 | NEWSCUM | pump_early_green_candle_sniper | pump_early_green_candle_sniper | LIQUIDITY_CRUSH | -73.34% | 0.00% | 26153.000 | 57.130 | 59.642 | 12415.090 |
| 35 | unckita | pump_early_green_candle_sniper | pump_early_green_candle_sniper | EARLY_DUMP_CUT | -37.18% | 0.00% | 34575.000 | 40.630 | 58.278 | 14277.320 |
| 36 | CLIPLIN | pump_early_green_candle_sniper | pump_early_green_candle_sniper | POST_PARTIAL_TRAILING | -61.34% | 41.89% | 46841.000 | 40.900 | 64.400 | 16706.790 |
| 39 | chudblin | pump_early_sniper_research | pump_early_late_momentum_watch | ADVERSE_TICK | -90.29% | 0.00% | 20173.570 | 750.000 | 55.571 | 2313.610 |
| 40 | Caretakers | pump_early_green_candle_sniper | pump_early_green_candle_sniper | EARLY_DUMP_CUT | -58.12% | 0.00% | 5282.980 | 86.040 | 56.840 | 2385.033 |
| 44 | $Lost | pump_early_green_candle_sniper | pump_early_green_candle_sniper | LIQUIDITY_CRUSH | -95.74% | 0.00% | 46174.980 | 274.000 | 56.922 | 11847.358 |
| 45 | HODLERS | pump_early_sniper_research | pump_early_research_rank_canary | ADVERSE_TICK | -25.63% | 0.00% | 62935.000 | 6.370 | 67.691 | 19892.760 |
| 46 | CHANCE | pump_early_green_candle_sniper | pump_early_green_candle_sniper | LIQUIDITY_CRUSH | -90.64% | 0.00% | 45823.000 | 33.050 | 58.124 | 16548.290 |
| 47 | CHUDMAN | pump_early_sniper_research | pump_early_research_rank_canary | LIQUIDITY_CRUSH | -68.50% | 0.00% | 36241.000 | 117.000 | 74.398 | 14866.910 |
| 48 | Nutcoin | pump_early_sniper_research | pump_early_research_rank_canary | LIQUIDITY_CRUSH | -57.79% | 0.00% | 70403.000 | 53.840 | 71.701 | 20673.890 |
| 51 | Guess | pump_early_green_candle_sniper | pump_early_green_candle_sniper | EARLY_DUMP_CUT | -49.29% | 0.00% | 12887.020 | 114.000 | 61.795 | 2569.721 |
| 55 | $Jollibee | pump_early_green_candle_sniper | pump_early_green_candle_sniper | LIQUIDITY_CRUSH | -72.95% | 0.00% | 5789.510 | 139.000 | 58.951 | 26521.228 |
| 59 | スネ | pump_early_green_candle_sniper | pump_early_green_candle_sniper | EARLY_DUMP_CUT | -57.74% | 0.00% | 5388.090 | 89.510 | 56.327 | 2529.011 |
| 63 | SOL | pump_early_sniper_research | pump_early_research_rank_canary | ADVERSE_TICK | -25.90% | 0.00% | 33041.000 | -19.780 | 62.837 | 14100.480 |
