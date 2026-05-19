# Untagged Buy Block

Paper buys without a valid entry lane, gate profile and lane tier are routed to shadow.

| Metric | Value |
|---|---:|
| Rows evaluated | 22835 |
| Blocked context rows | 22794 |
| Runtime blocked events | 196 |

## Blocked Reasons

- `untagged_standard_buy_disabled`: 22794
- `profit_lane_tier_missing`: 21945
- `gate_profile_missing`: 18965
- `pumpfun_standard_buy_disabled`: 16582
- `entry_lane_missing`: 14586
- `sniper_research_subprofile_missing`: 1357
- `dex_mature_standard_buy_disabled`: 66
- `pumpswap_prime_not_strict`: 1
