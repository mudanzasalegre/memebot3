# Untagged Buy Block

Paper buys without a valid entry lane, gate profile and lane tier are routed to shadow.

| Metric | Value |
|---|---:|
| Rows evaluated | 55544 |
| Blocked context rows | 55526 |
| Runtime blocked events | 306 |

## Blocked Reasons

- `untagged_standard_buy_disabled`: 55526
- `profit_lane_tier_missing`: 54667
- `gate_profile_missing`: 51285
- `pumpfun_standard_buy_disabled`: 48654
- `entry_lane_missing`: 46294
- `sniper_research_subprofile_missing`: 275
- `dex_mature_standard_buy_disabled`: 64
