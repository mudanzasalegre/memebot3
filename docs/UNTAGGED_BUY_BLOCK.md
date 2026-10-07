# Untagged Buy Block

Paper buys without a valid entry lane, gate profile and lane tier are routed to shadow.

| Metric | Value |
|---|---:|
| Rows evaluated | 125052 |
| Blocked context rows | 123154 |
| Runtime blocked events | 515 |

## Blocked Reasons

- `untagged_standard_buy_disabled`: 123154
- `profit_lane_tier_missing`: 121186
- `gate_profile_missing`: 96042
- `entry_lane_missing`: 86841
- `pumpfun_standard_buy_disabled`: 58047
- `dex_mature_standard_buy_disabled`: 44735
- `sniper_research_subprofile_missing`: 2936
- `pumpswap_profit_not_prime`: 105
- `pumpswap_prime_not_strict`: 69
