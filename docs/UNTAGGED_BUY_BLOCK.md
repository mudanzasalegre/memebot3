# Untagged Buy Block

Paper buys without a valid entry lane, gate profile and lane tier are routed to shadow.

| Metric | Value |
|---|---:|
| Rows evaluated | 1314 |
| Blocked context rows | 1314 |
| Runtime blocked events | 64 |

## Blocked Reasons

- `untagged_standard_buy_disabled`: 1314
- `profit_lane_tier_missing`: 1265
- `gate_profile_missing`: 1083
- `entry_lane_missing`: 841
- `pumpfun_standard_buy_disabled`: 789
- `sniper_research_subprofile_missing`: 33
- `dex_mature_standard_buy_disabled`: 15
