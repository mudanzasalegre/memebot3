# Untagged Buy Block

Paper buys without a valid entry lane, gate profile and lane tier are routed to shadow.

| Metric | Value |
|---|---:|
| Rows evaluated | 248130 |
| Blocked context rows | 247287 |
| Runtime blocked events | 268 |

## Blocked Reasons

- `untagged_standard_buy_disabled`: 247287
- `profit_lane_tier_missing`: 246470
- `gate_profile_missing`: 237676
- `entry_lane_missing`: 229445
- `pumpfun_standard_buy_disabled`: 225814
- `sniper_research_subprofile_missing`: 633
- `dex_mature_standard_buy_disabled`: 259
