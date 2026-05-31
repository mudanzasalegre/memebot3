# Untagged Buy Block

Paper buys without a valid entry lane, gate profile and lane tier are routed to shadow.

| Metric | Value |
|---|---:|
| Rows evaluated | 45791 |
| Blocked context rows | 45181 |
| Runtime blocked events | 378 |

## Blocked Reasons

- `untagged_standard_buy_disabled`: 45181
- `profit_lane_tier_missing`: 43568
- `gate_profile_missing`: 37808
- `pumpfun_standard_buy_disabled`: 33659
- `entry_lane_missing`: 29412
- `sniper_research_subprofile_missing`: 623
- `dex_mature_standard_buy_disabled`: 94
- `pumpswap_prime_not_strict`: 2
