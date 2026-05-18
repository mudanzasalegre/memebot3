# Untagged Buy Block

Paper buys without a valid entry lane, gate profile and lane tier are routed to shadow.

| Metric | Value |
|---|---:|
| Rows evaluated | 124318 |
| Blocked context rows | 124215 |
| Runtime blocked events | 925 |

## Blocked Reasons

- `untagged_standard_buy_disabled`: 124215
- `profit_lane_tier_missing`: 119711
- `gate_profile_missing`: 103610
- `pumpfun_standard_buy_disabled`: 89599
- `entry_lane_missing`: 79302
- `sniper_research_subprofile_missing`: 7901
- `dex_mature_standard_buy_disabled`: 308
- `pumpswap_prime_not_strict`: 30
- `pumpswap_profit_not_prime`: 3
