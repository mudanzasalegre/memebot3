# Untagged Buy Block

Paper buys without a valid entry lane, gate profile and lane tier are routed to shadow.

| Metric | Value |
|---|---:|
| Rows evaluated | 101547 |
| Blocked context rows | 99829 |
| Runtime blocked events | 511 |

## Blocked Reasons

- `untagged_standard_buy_disabled`: 99829
- `profit_lane_tier_missing`: 97910
- `gate_profile_missing`: 75793
- `entry_lane_missing`: 54876
- `pumpfun_standard_buy_disabled`: 47145
- `sniper_research_subprofile_missing`: 1934
- `dex_mature_standard_buy_disabled`: 464
