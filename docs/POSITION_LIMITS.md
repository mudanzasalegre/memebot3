# Position Limits

Lane caps use explicit semantics:

| Cap | Meaning |
|---:|---|
| `-1` | Unlimited open positions for that lane. |
| `0` | Blocked; no new positions can open. |
| `N > 0` | Allow while current open count is below `N`. |

This matters most for live canaries. For example, `LATE_MOMENTUM_WATCH_MAX_OPEN_LIVE=0` means late momentum cannot open live positions.

`runtime.position_limits.evaluate_lane_position_limit()` returns the normalized lane, current lane open count, configured cap and an allow/block reason.
