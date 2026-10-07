# Auditoría exhaustiva del bot — run `20260710-1`

Generado el 2026-07-12. Audiencia: propietario/operador de memebot3. Estado final del sistema: **operativo en paper, no aprobado para live**.

## Resumen ejecutivo

La ejecución auditada no fue ganadora. Cerró 24 posiciones con un PnL **gross spot** de **-$1,808188** sobre **$14,021230** de notional (**-12,8961%**). Ganó 5 de 24 operaciones (20,83%), tuvo expectancy de -$0,07534 por trade y profit factor 0,338. El PnL no descuenta fees de red, prioridad, tips ni slippage ejecutado; el neto económico sería igual o peor.

La pérdida no fue aleatoria ni estuvo repartida uniformemente:

- `shadow_followup_micro` produjo 17 trades, **-$1,387600**, ROI sobre notional **-35,0880%**, 3 ganadores y 9 pérdidas severas.
- `paper_bootstrap_micro` produjo 7 trades, **-$0,420588**, ROI sobre notional **-4,1781%**, 2 ganadores y 1 pérdida severa. Su promedio porcentual simple fue +1,00%, pero está contradicho por el resultado ponderado en dólares; el sizing hizo que perdiera dinero.
- 10 salidas `MAX_ADVERSE_EXCURSION` y 6 `LIQUIDITY_CRUSH` explican **98,74% de todas las pérdidas brutas**. Las seis crisis de liquidez promediaron -79,11%.
- 21 de 24 posiciones tenían `cluster_bad`; las 17 compras de `shadow_followup` pertenecían al patrón `real_liquidity_breakout` (14 etiquetadas y 3 equivalentes sin etiqueta).

No hay base estadística para afirmar que el bot ya sea ganador. La corrección responsable ha sido cortar las fuentes demostrablemente negativas, reparar la medición y dejar que AutoResearch encuentre evidencia causal nueva en paper. `DRY_RUN=1`, el lock de optimización y todas las promociones live permanecen activos.

## Métricas de la ejecución

| Métrica | Valor |
|---|---:|
| Ventana | 2026-07-10 22:23 UTC → 2026-07-12 11:47 UTC (~37,4 h) |
| Candidatos únicos en funnel | 17.792 |
| Compras / posiciones | 24 / 24 |
| Eventos sell | 31 (incluye 7 parciales) |
| Posiciones abiertas al cierre | 0 |
| Notional desplegado | $14,021230 |
| PnL gross spot | -$1,808188 |
| Retorno sobre notional | -12,8961% |
| PnL medio / mediano por trade | -24,5097% / -20,9350% |
| Win rate | 20,8333% |
| Expectancy | -$0,075341 por trade |
| Profit factor | 0,337690 |
| Máximo drawdown secuencial | $2,088315 |
| Confianza de precio al cierre | `low` en 24/24 |

## Los 24 trades

| # | Cierre UTC | Token | Lane | Salida | Notional $ | PnL $ | PnL % | Cum. $ | Cluster | Parciales |
|---:|---|---|---|---|---:|---:|---:|---:|:---:|---:|
| 1 | 2026-07-10 22:25 | Kiri | shadow_followup | MAX_ADVERSE_EXCURSION | 0.23394 | -0.037260 | -15.93% | -0.037260 | yes | 0 |
| 2 | 2026-07-10 22:48 | HOODCAT | shadow_followup | POST_PARTIAL_TRAILING | 0.23394 | +0.054161 | +23.15% | +0.016901 | yes | 1 |
| 3 | 2026-07-11 00:34 | SAPPHIRE | shadow_followup | LIQUIDITY_CRUSH | 0.23373 | -0.229405 | -98.15% | -0.212504 | yes | 0 |
| 4 | 2026-07-11 01:17 | thenorth | shadow_followup | MAX_ADVERSE_EXCURSION | 0.23331 | -0.099060 | -42.46% | -0.311564 | yes | 0 |
| 5 | 2026-07-11 01:23 | bridgoor | paper_bootstrap | DYNAMIC_RUNNER_FLOOR | 0.77920 | +0.563868 | +72.36% | +0.252304 | yes | 3 |
| 6 | 2026-07-11 01:27 | smirk | paper_bootstrap | MAX_ADVERSE_EXCURSION | 0.77870 | -0.257332 | -33.05% | -0.005028 | yes | 0 |
| 7 | 2026-07-11 02:52 | ven | paper_bootstrap | NO_PUMP_EXIT | 0.77600 | -0.000180 | -0.02% | -0.005209 | yes | 0 |
| 8 | 2026-07-11 04:54 | NPAC | shadow_followup | POST_PARTIAL_TRAILING | 0.23298 | +0.103118 | +44.26% | +0.097909 | yes | 2 |
| 9 | 2026-07-11 06:35 | meowcoin | shadow_followup | LIQUIDITY_CRUSH | 0.23367 | -0.154252 | -66.01% | -0.056343 | yes | 0 |
| 10 | 2026-07-11 07:00 | ANSEMINU | shadow_followup | LIQUIDITY_CRUSH | 0.23406 | -0.227373 | -97.14% | -0.283716 | yes | 0 |
| 11 | 2026-07-11 08:12 | TREASURY | shadow_followup | MAX_ADVERSE_EXCURSION | 0.23379 | -0.085388 | -36.52% | -0.369104 | yes | 0 |
| 12 | 2026-07-11 08:24 | Chance | shadow_followup | LIQUIDITY_CRUSH | 0.23388 | -0.229145 | -97.98% | -0.598249 | yes | 0 |
| 13 | 2026-07-11 08:57 | MemesAI | paper_bootstrap | TRAILING_STOP | 0.77990 | -0.009072 | -1.16% | -0.607320 | yes | 0 |
| 14 | 2026-07-11 15:18 | CUM | shadow_followup | MAX_ADVERSE_EXCURSION | 0.23547 | -0.053497 | -22.72% | -0.660817 | yes | 0 |
| 15 | 2026-07-11 16:48 | echo | shadow_followup | MAX_ADVERSE_EXCURSION | 0.23430 | -0.044870 | -19.15% | -0.705687 | yes | 0 |
| 16 | 2026-07-11 17:09 | ANSUM | paper_bootstrap | TRAILING_STOP | 2.33340 | +0.090536 | +3.88% | -0.615151 | no | 0 |
| 17 | 2026-07-11 17:09 | TRUMPVAULT | paper_bootstrap | MAX_ADVERSE_EXCURSION | 2.33580 | -0.415958 | -17.81% | -1.031110 | no | 0 |
| 18 | 2026-07-11 18:24 | VIPERS | shadow_followup | LIQUIDITY_CRUSH | 0.23403 | -0.125026 | -53.42% | -1.156135 | yes | 0 |
| 19 | 2026-07-12 01:17 | Bellingoal | paper_bootstrap | MAX_ADVERSE_EXCURSION | 2.28360 | -0.392449 | -17.19% | -1.548585 | no | 0 |
| 20 | 2026-07-12 01:29 | HALLWAY | shadow_followup | MAX_ADVERSE_EXCURSION | 0.22854 | -0.144486 | -63.22% | -1.693071 | yes | 0 |
| 21 | 2026-07-12 04:11 | McLegger | shadow_followup | LIQUIDITY_CRUSH | 0.23073 | -0.142940 | -61.95% | -1.836011 | yes | 0 |
| 22 | 2026-07-12 05:27 | WHO | shadow_followup | POST_PARTIAL_TRAILING | 0.23001 | +0.110253 | +47.93% | -1.725758 | yes | 2 |
| 23 | 2026-07-12 05:27 | ギコ | shadow_followup | MAX_ADVERSE_EXCURSION | 0.22968 | -0.057400 | -24.99% | -1.783158 | yes | 0 |
| 24 | 2026-07-12 06:11 | UNC | shadow_followup | PRE_PARTIAL_TIME_STOP | 0.22857 | -0.025030 | -10.95% | -1.808188 | yes | 0 |

## Oportunidades y funnel

De 17.792 candidatos únicos, 24 acabaron comprados (0,135%), 7.088 quedaron en shadow y 10.680 fueron rechazados. Se identificaron 37 direcciones no compradas con pico posterior ≥100%, 3 con pico ≥500% y 1 con pico ≥1.000%.

Esto no justifica relajar filtros indiscriminadamente. Entre los mayores “missed winners” aparecen simultáneamente `cluster_bad`, liquidez proxy, ruta no ejecutable, bajo número de transacciones y alto impacto. Esos picos son evidencia de oportunidad, pero no de fills ejecutables ni de rentabilidad neta. El camino implementado conserva esos candidatos en research/shadow, exige ruta real para cualquier compra y permite que AutoResearch pruebe reglas sobre timestamps causales.

Principales bloqueadores del funnel: `vol_low`, `soft_score`, `max_age`, `dedup`, `mcap_low`, calidad de snapshot/proxy de liquidez y presión de venta tóxica. El sistema recogió mucha actividad, pero no la convirtió en evidencia ejecutable de alta calidad.

## Calidad de datos y proveedores

- Feature store: 7.624 filas. Cobertura nula (0%) para `holders`, `rug_score`, `social_ok`, `price_pct_1m` y `volume_pct_5m`.
- Social enrichment: 2.173/2.173 filas `unknown`; 0 enlaces útiles.
- Los datos de liquidez, volumen 24h, market cap, txns 5m y price 5m tienen coberturas aproximadas de 92,84%, 97,60%, 97,67%, 89,34% y 78,56% respectivamente.
- Logs: 33.057 respuestas Birdeye 404, 13.270 señales de incompletitud y 17 desconexiones PumpPortal. El provider health sigue `critical` por Birdeye/completitud.
- Los contadores anteriores de 429 eran falsos positivos por buscar la cadena `429` en JSON arbitrario. El parser estricto reconstruye **0** Gecko 429, **0** Birdeye 429 y **0** Jupiter rate-limit en el run.
- Las 22 excepciones `_write_event() got multiple values for argument 'event_type'` se originaban al permitir que contexto no confiable sobrescribiera campos reservados.

Conclusión de calidad: los trades/PnL SQLite son utilizables para diagnosticar la ejecución, pero la evidencia de features sociales/on-chain y el PnL neto no son suficientes para activar live ni un modelo de bloqueo.

## Modelos y AutoResearch

El entrenamiento final produjo 25 candidatos. El más reciente, `20260712_153617_lightgbm_small`, tiene ROC-AUC 0,8760 y PR-AUC 0,3471, pero **no está listo para activación**: precision al threshold 0,4043 frente al floor 0,60. La validación tuvo que usar walk-forward agrupado por mint porque una separación forward de tres días era imposible en una ventana de ~37 h.

De las 7.624 filas de features, 5.195 son `policy_reject`, 2.405 `shadow_close` y solo 24 `trade_close`. Tras aplicar el allowlist productivo, las 2.222 filas elegibles son todas `shadow_close`: ninguna ejecución real entra en el training set. Por ello el modelo puede priorizar research, pero no debe decidir dinero real. Los modelos de riesgo y EV no existen y el sizing dependiente de ellos devolvía valores nulos; las tres rutas se han desactivado explícitamente.

AutoResearch acumula 98 candidatas históricas: 70 rechazadas y 28 fallidas, 0 aceptadas. Los fallos históricos incluían timeouts y falsos 429. Tras la reparación se ejecutó un ciclo real con una candidata: `failed=0`, API 429=0, estado `completed`; la candidata fue rechazada correctamente por no producir efecto causal ni trades aceptables. La siguiente puesta en marcha detectó el ciclo reciente y lo omitió sin error.

## Cambios implementados

1. **Estrategia y ejecución**
   - `paper_bootstrap`, `shadow_followup` y `real_liquidity_breakout` quedan desactivados en `paper_hotfix_0707` por expectancy negativa.
   - Los escapes de `cluster_bad` son opt-in y permanecen falsos.
   - Bootstrap se revalida tras enriquecimiento de riesgo y justo antes de ejecutar; sus quality gates y caps ya son configuración real.
   - “Jupiter tiene precio” ya no equivale a “Jupiter tiene ruta”. Ruta ausente o desconocida bloquea.
   - El replay aplica el mismo sizing final de lane y no puede usar fills proxy por defecto.

2. **Causalidad, PnL y reportes**
   - SQLite es la verdad autoritativa para trades ejecutados; outcomes shadow contradictorios se suprimen.
   - No se retrotraen señales a `first_seen_at`, `closed=false` no cierra trades y candidate partial no cuenta como cierre.
   - SOL y USD están separados; no se inventa PnL USD sin notional.
   - Latencia, slippage y route-proxy son supuestos protegidos que una candidata no puede optimizar.
   - La API/UI llama al resultado `gross_spot_closed_pnl_usd` y conserva el alias antiguo solo por compatibilidad.

3. **AutoResearch y ML**
   - Regeneración por candidata aislada del root; no sobrescribe reports/baselines compartidos.
   - 429 y provider health se detectan con señales estrictas y sin duplicar mirrors.
   - Ciclos con fallos parciales quedan `degraded`; si todos fallan quedan `failed`, con `last_error` útil.
   - Features se escriben con lock interproceso y reemplazo atómico.
   - Thresholds globales se publican solo tras promoción; promoción requiere el booleano literal `activation_ready=true` además del lock.
   - Añadido `strategy_proposals/schema.autoresearch.json` y cap moonshot `MAX_OPEN=1`; quality gate final `ok`.

4. **Persistencia y lifecycle**
   - Cualquier sell confirmado cuyo commit DB falle se captura antes del commit en journal JSONL con `fsync`.
   - El replay del journal es idempotente, restaura partial/close completo, pausa buys/discovery y bloquea resume hasta resolver.
   - Journal corrupto falla cerrado salvo una última línea truncada por crash.
   - El launcher limpia perfiles heredados, espera health real de API y mata el árbol si falla readiness.
   - `stop_stack` termina con `stack_processes=0`, actualiza SQLite a `process_state=stopped` y elimina el lock muerto.

## Validación final

- Suite completa: **745 passed**.
- `strategy_quality_gate=ok`.
- Regeneración completa: 34 reports, warnings vacíos.
- AutoResearch smoke aislado: `status=ok`, 3 candidatas, live siempre false.
- Ciclo AutoResearch real: 1 completada, 0 failed, 0 API 429; rechazo causal correcto.
- API: `/api/v1/health` → `ok`, `degraded=false`.
- UI: HTTP 200.
- Bot: `DRY_RUN=True`, 0 líneas ERROR/CRITICAL en el log del arranque final.
- Stop final: `stack_processes=0`, SQLite `process_state=stopped`, `run_bot.lock` ausente.

## Decisión y criterios para dinero real

**Decisión actual: NO-GO live.** Se ha detenido el sangrado y el sistema vuelve a medir/experimentar de forma fiable, pero todavía no hay una política con expectativa positiva validada.

Antes de habilitar dinero real deben cumplirse todos estos puntos en una ventana nueva, no reutilizando el run auditado:

1. Una candidata AutoResearch con ≥5 cierres causales ejecutables en replay y después forward paper suficiente.
2. PnL neto después de fees/slippage/tips > 0, profit factor > 1 y drawdown dentro del presupuesto.
3. Sin incremento de `LIQUIDITY_CRUSH`, severe losses, 429 ni degradación de proveedores.
4. Route coverage real y confidence de precio no `low` en las entradas/salidas usadas como evidencia.
5. Modelo `activation_ready=true` con precision ≥0,60 y evaluación que incluya fills reales; hasta entonces solo shadow.
6. Desbloqueo live manual, canary mínimo y rollback automático; nunca promoción live automática.

## Fuentes y metodología

- `data/memebotdatabase.db`: posiciones, eventos de trade y tokens, filtrados por `run_id='20260710-1'`.
- `data/metrics/runtime_events.jsonl`, `candidate_outcomes.jsonl`, `decision_ledger.jsonl`, `social_enrichment.jsonl`.
- `data/features/features_202607.parquet` y artefactos bajo `ml/models/`.
- `data/research_runs/scoreboard.json`, `autoresearch_events.jsonl`, runtime state y report bundles.
- Logs horarios bajo `logs/`.
- Cálculos: PnL gross spot = suma de `positions.total_pnl_usd`; ROI = PnL / notional; profit factor = ganancias brutas / pérdidas brutas; drawdown sobre la secuencia de cierres. Todos los conteos de trades se deduplican por posición ejecutada.
