# Untagged Buy Block

Paper buys without a valid entry lane, gate profile and lane tier are routed to shadow.

| Metric | Value |
|---|---:|
| Rows evaluated | 101870 |
| Blocked context rows | 101817 |
| Runtime blocked events | 714 |

## Blocked Reasons

- `untagged_standard_buy_disabled`: 101817
- `profit_lane_tier_missing`: 98119
- `gate_profile_missing`: 84998
- `pumpfun_standard_buy_disabled`: 73565
- `entry_lane_missing`: 65098
- `sniper_research_subprofile_missing`: 6469
- `dex_mature_standard_buy_disabled`: 237
- `pumpswap_prime_not_strict`: 25
- `pumpswap_profit_not_prime`: 3
