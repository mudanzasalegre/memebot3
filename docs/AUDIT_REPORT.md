# Audit Report

- Generated at UTC: `2026-04-15T18:19:07.254946+00:00`
- Project root: `D:\Dev\Python\memebot3`

## Live Baseline

- Closed trades: `193`
- Open trades: `0`
- Total PnL USD: `3.26588088`
- Win rate: `41.451`
- Avg PnL (%): `2.095`
- Median PnL (%): `-6.95`
- Latest closed at: `2026-04-15T17:42:16.838147+00:00`

## Ledger Consistency

- DB closed rows: `193`
- Paper closed rows: `193`
- Scorecard live closed: `193`
- Lag rows: `0`
- Is consistent: `True`

## Pump Early Sweeps

### Entry Filter

- Baseline count: `193`
- Best params: `{'min_liquidity_usd': 0.0, 'min_volume_24h_usd': 0.0, 'max_price_impact_pct': 10.0, 'max_snapshot_missing_fields': None}`
- Best total PnL USD: `4.80737555`
- Best avg PnL (%): `3.209`
- Best drawdown: `-461.462`
- Guardrail passed: `True`

### Requeue Cap

- Baseline count: `193`
- Best params: `{'max_strategy_confirm_snapshots_requeues': 2, 'max_no_liq_requeues': 2}`
- Best total PnL USD: `3.26588088`
- Best avg PnL (%): `2.095`
- Best drawdown: `-599.978`
- Guardrail passed: `True`

### Post-Partial Protection

- Baseline count: `193`
- Best params: `{'lock_floor_pct': 20.0, 'max_giveback_after_partial_pct': 5.0}`
- Best total PnL USD: `23.90975754`
- Best avg PnL (%): `14.924`
- Best drawdown: `-396.158`
- Guardrail passed: `True`

## Research Dataset

- Portfolio rows: `91`
- Candidate rows in: `5269`
- Candidate rows out: `5269`
- Ambiguous bought rows dropped: `0`
- Source counts: `{'candidate_decision': 3679, 'candidate_stage': 1271, 'live_trade': 193, 'research_shadow': 90, 'candidate_partial': 36}`

## Artifact Freshness

- Research scorecard: updated_at=`2026-04-15T18:00:01.154863+00:00`, stale_vs_live_close=`False`
- Research thresholds: updated_at=`2026-04-15T18:00:01.154863+00:00`, stale_vs_live_close=`False`
- Edge report: updated_at=`2026-04-15T18:08:49.945947+00:00`, stale_vs_live_close=`False`
- ML report: updated_at=`2026-04-15T18:08:47.892831+00:00`, stale_vs_live_close=`False`

## Log Noise

- Log files: `119`
- Levels: `{'INFO': 30629, 'DEBUG': 1254366, 'WARNING': 2656}`

- Warning `1671`x: `Campos críticos nulos volume_24h_usd,liquidity_usd`
- Warning `884`x: `Campos críticos nulos liquidity_usd`
- Warning `50`x: `Campos críticos nulos volume_24h_usd`
- Warning `25`x: `[PumpFun] WS desconectado (WS closed). Reintentando en 2s…`
- Warning `10`x: `[jupiter_price] Token <token> sin precio tras varios intentos – posiblemente no soportado aún`
- Warning `3`x: `[GT] cooldown global 60s tras 429`
- Warning `2`x: `[PumpFun] WS desconectado (Cannot write to closing transport). Reintentando en 2s…`
- Warning `1`x: `[jupiter_price] https://lite-api.jup.ag/price/v3 → 502`
