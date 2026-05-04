# Post-adjustment Policy Replay

| Policy | Trades | Win rate | Avg PnL | Total PnL | Delta PnL | Severe | Delta Severe | Runner capture |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline_48h | 23380 | 2.58% | 0.32% | 7566.32 | 0.00 | 450 | 0 | 0.018 |
| research_rank_priority | 23380 | 2.58% | 0.32% | 7566.32 | 0.00 | 450 | 0 | 0.018 |
| green_sniper_shadow_first | 23380 | 2.26% | 0.49% | 11424.06 | 3857.74 | 376 | -74 | 0.017 |
| green_sniper_restricted | 23380 | 2.26% | 0.49% | 11356.61 | 3790.28 | 377 | -73 | 0.017 |
| late_momentum_research_only | 23380 | 2.53% | 0.36% | 8519.03 | 952.71 | 427 | -23 | 0.018 |
| post_partial_protected | 23380 | 2.58% | 0.73% | 17018.24 | 9451.91 | 450 | 0 | 0.019 |
| early_dump_candidates | 23380 | 2.58% | 1.17% | 27365.58 | 19799.26 | 7 | -443 | 0.018 |
| combined_adjusted_v1 | 23380 | 2.22% | 1.47% | 34345.83 | 26779.51 | 6 | -444 | 0.018 |

## Frozen 48h Baseline Reference

- Closed trades: `63`
- Win rate: `31.746`
- Avg PnL: `27.697`
- Severe losses: `22`
