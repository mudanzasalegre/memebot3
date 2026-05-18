# Post Hotfix Strategy Preview

Offline preview for `combined_hotfix_v1`. Live remains disabled; this report does not promote models or change wallets.

## Combined Estimate

- Baseline closed rows: `60`
- Expected total PnL delta: `14014.9952` pct-points
- Expected severe loss delta: `-1`
- Expected runner capture delta: `0.001600`
- Estimate note: Offline additive preview. Entry and exit effects can overlap; use as directional validation before paper forward.

## Entry Changes

| Surface | Count | Total PnL delta | Severe delta | Peak100 | Peak500 |
|---|---:|---:|---:|---:|---:|
| Pumpswap strict blocked current | 1 | 23.3472 | -1 | 0 | 0 |
| Rebound incremental candidates | 0 | 0.0000 | 0 | 0 | 0 |
| Birth micro candidates | 0 | 0.0000 | 0 | 0 | 0 |

## Exit Changes

- Post-partial expected total delta: `13991.6480`
- Multi-partial runner rows: `0`
- Multi-partial expected total delta: `0.0000`
- Emergency sells simulated: `0`

## Research Rank Lane

- Audit evaluated: `6703`
- Bought as own lane: `12`
- Shadow as own lane: `0`
- Mixed lane detected: `0`

## Safety

- `dry_run`: `True`
- `strategy_optimization_lock`: `True`
- `live_canary_enabled`: `False`
- `auto_promote_live`: `False`
- `model_auto_promote`: `False`
- `birth_probe_micro_live_enabled`: `False`
- `bird_runner_live_enabled`: `False`
- `runner_giveback_live_enabled`: `False`
