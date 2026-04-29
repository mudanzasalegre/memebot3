# Replay And Metrics

`missed_pumps` now separates visible momentum from confirmed later outcomes. `price_pct_5m_at_seen` is not a later peak.

Use `policy_replay` as the acceptance gate. `combined_v1` should improve total PnL and reduce severe losses before any live change.

Track runner capture via `runner_capture_ratio`, `giveback_pct`, and runner buckets `>50`, `>100`, `>300`.
