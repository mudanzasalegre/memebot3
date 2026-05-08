# Runner Jackpot Report

## Summary

- Rows analysed: 3865
- Runners >=300: 9
- Runners >=500: 5
- Runners >=1000: 0

## Top Runners

| Symbol | Peak | Realized | Lane | Gate | Exit | Liquidity | Mcap | Price5m | Txns |
|---|---:|---:|---|---|---|---:|---:|---:|---:|
| HDeuwQ | 887.1% | 80.7% | pump_early_green_candle_sniper | green_sniper | POST_PARTIAL_TRAILING | 2400 | 10777 | 0.0 | 0 |
| CHIMP | 628.9% | 628.9% | pump_early_sniper_research | pumpswap_profit_research | TIMEOUT_HARD | 19317 | 61534 | 46.1 | 784 |
| CHIMP | 628.9% | 628.9% | pump_early_sniper_research | pumpswap_profit_research | TIMEOUT_HARD | 19317 | 61534 | 46.1 | 784 |
| HDeuwQ | 581.6% | 581.6% |  |  |  | 0 | 0 | 0.0 | 0 |
| CZwtNu | 512.4% | 410.2% | pump_early_green_candle_sniper | green_sniper_birth_probe | TRAILING_STOP | 1200 | 5468 | 0.0 | 0 |
| HYmXAo | 411.9% | 411.9% |  |  |  | 0 | 0 | 0.0 | 0 |
| HYmXAo | 411.9% | 411.9% | pump_early_green_candle_sniper | green_sniper_birth_probe | TIMEOUT_HARD | 1200 | 0 | 0.0 | 0 |
| PLECO | 353.8% | 125.9% |  |  | POST_PARTIAL_TRAILING | 2423 | 8043 | 0.3 | 177 |
| PLECO | 353.8% | 125.9% |  |  | POST_PARTIAL_TRAILING | 2423 | 8043 | 0.3 | 177 |

## Applied Policy

The `jackpot_runner` profile is for the real-liquidity research-rank pattern that produced the largest confirmed runner.
It sells a smaller first partial and then tightens lock floors after +100%, +300% and +500% peaks.
