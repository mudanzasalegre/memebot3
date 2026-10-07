# Auditoría exhaustiva de MemeBot3 — run `20260713-1`

Fecha de auditoría: 2026-07-15. Ámbito: última ejecución completa congelada, sus datos, logs, modelos, AutoResearch, flujo de decisión y ciclo de vida del stack.

Estado de entrega: **código corregido y validado en paper; NO-GO para live**.

## Resumen ejecutivo

El run `20260713-1` no compró porque el sistema **no llegó a intentar ninguna compra**: SQLite contiene 0 posiciones y 0 trades, y la telemetría contiene 0 intentos de ejecución y 0 compras. No fue un problema de una orden rechazada por Jupiter, de saldo, de firma o de RPC. Las decisiones quedaron bloqueadas antes de la capa de compra.

El perfil efectivo `config/profiles/paper_hotfix_0707.env` desactivaba las vías que podían comprar en paper —especialmente bootstrap, shadow follow-up y exploración— mientras que el rank dejó pasar solo dos casos, que después también fueron bloqueados. El bot descubrió candidatos y evaluó miles de decisiones, pero su configuración no permitía convertirlas en órdenes paper.

Tampoco habría sido correcto resolverlo abriendo todos los filtros. Los 2.353 outcomes shadow terminales tuvieron un retorno medio de **-4,557907%**, win rate de **8,584785%** y profit factor de **0,483938**. Comprarlos todos a 0,1 SOL habría producido hipotéticamente **-10,724754 SOL antes de costes**. La evidencia confirma que había oportunidades puntuales, pero que el universo general era perdedor y muy tóxico.

Una reconstrucción causal post-hoc identificó siete casos que cumplen una política mucho más selectiva. Seis fueron positivos y su resultado hipotético agregado fue **+0,213305 SOL** a 0,1 SOL por posición, antes de fees, tips, slippage real y fallos de fill. Esta muestra es demasiado pequeña, fue seleccionada retrospectivamente y perdió el valor histórico explícito de `liquidity_is_proxy`; por tanto, **no demuestra una ventaja operable ni autoriza dinero real**. Solo justifica una prueba prospectiva en paper con gates estrictos y telemetría reparada.

Se ha implementado esa política prospectiva en paper con tamaño exacto de **0,1 SOL**, se han reparado fallos de cola, lifecycle, arranque y contratos de datos, y se han desactivado rutas alternativas que podían contaminar la prueba. La suite completa termina con **783 tests aprobados**. El ciclo real final `20260715-8` alcanzó `running`, descubrió candidatos en aproximadamente un segundo, evaluó el bootstrap por 0,1 SOL desde `entry_lane_guard`, ejecutó el monitor sin errores y se detuvo con `remaining=0`, sin lock y con SQLite en `stopped/idle/idle`.

No existe una modificación responsable que garantice beneficio o “máximo beneficio”. El resultado implementado maximiza la calidad de selección dentro de la evidencia disponible, limita exposición y crea una prueba falsable. La activación live seguirá bloqueada hasta acumular suficiente evidencia forward, con costes reales modelados y criterios de aceptación cumplidos.

## Alcance y congelación del run

| Campo | Valor |
|---|---|
| `run_id` | `20260713-1` |
| Inicio | `2026-07-13T12:06:01Z` |
| Último evento auditado | aproximadamente `2026-07-15T00:26:49Z` |
| Duración efectiva | aproximadamente 36,35 horas |
| PID registrado | `14936`, ya inexistente al auditar |
| Lock encontrado | `data/run_bot.lock`, obsoleto |
| Estado SQLite encontrado | `running`, obsoleto respecto al proceso real |
| Perfil efectivo | `config/profiles/paper_hotfix_0707.env` |
| Modo | paper / `DRY_RUN=1` |

La auditoría congeló el run antes de modificar la política. Las conclusiones de rendimiento no mezclan eventos de los arranques de validación posteriores `20260715-2` a `20260715-8`.

## Resultado observado y funnel completo

| Métrica | Resultado |
|---|---:|
| Eventos de runtime | 100.598 |
| Descubrimientos raw | 15.198 |
| Candidatos únicos reconstruidos en funnel | 15.589 |
| Rechazados | 8.642 (55,44%) |
| Shadow | 6.947 (44,56%) |
| Filas en decision ledger | 15.991 |
| IDs únicos en decision ledger | 15.977 |
| Duplicados de ID | 14 |
| Bloqueos antes de compra | 13.256 |
| Intentos de compra | **0** |
| Compras / posiciones / trades | **0 / 0 / 0** |

Los estados del ledger se distribuyeron en 5.992 `delay`, 5.133 `shadow`, 4.780 `reject` y 86 `execution_blocked`. Los principales stages de bloqueo previo a compra fueron:

| Stage | Casos |
|---|---:|
| `basic_filter` | 5.984 |
| `soft_score` | 3.132 |
| `green_sniper` | 1.955 |
| `entry_quality` | 1.139 |

El sistema estaba activo como descubridor y evaluador, no como comprador paper. No hay evidencia de que una orden alcanzara el executor y fallara allí.

## Causa raíz de las cero compras

La causa es de control de política, reforzada por problemas de observabilidad y lifecycle:

1. **Bootstrap estaba desactivado.** Se registraron 11.308 evaluaciones bloqueadas en la vía que podía convertir candidatos válidos en compras paper.
2. **Shadow follow-up estaba desactivado.** Se bloquearon 4.926 evaluaciones de esta vía.
3. **Exploración estaba desactivada.** Se bloquearon 163 evaluaciones exploratorias.
4. **Rank no sustituyó esas vías.** Solo dos candidatos superaron su corte inicial; ambos presentaban `price_pct_5m` negativo y quedaron bloqueados antes de ejecutar.
5. **ML no fue el bloqueador principal.** Hubo 4.928 decisiones ML con `passed=true`, pero `enforced=false`; el modelo estaba en shadow.
6. **El proceso murió sin cerrar limpiamente.** Quedaron lock y estado SQLite obsoletos. Esto no explica las cero compras durante las 36 horas, pero sí podía provocar un primer arranque posterior ambiguo o fallido.

Existía además una contradicción de sizing: `.env` declaraba `TRADE_AMOUNT_SOL=0.1`, mientras que el perfil efectivo limitaba la cantidad paper a 0,03 SOL y mantenía cantidades micro en otras lanes. Aunque ninguna compra llegó a ejecutarse, esa divergencia impedía garantizar la intención operativa de 0,1 SOL.

La solución no ha sido habilitar todo. Se ha habilitado una única lane prospectiva acotada, se han fijado sus invariantes en preflight y se han mantenido todas las rutas live cerradas.

## Auditoría de todas las oportunidades shadow

Se enlazaron los 2.353 outcomes shadow terminales con el último snapshot del decision ledger disponible **antes** de su `opened_at`. La unión fue completa:

| Calidad de join causal | Valor |
|---|---:|
| Outcomes terminales enlazados | 2.353 / 2.353 |
| Sin match | 0 |
| Lag p50 | 0 s |
| Lag p90 | 0,003519 s |
| Lag máximo | 0,4177 s |

### Universo general

| Métrica | Resultado |
|---|---:|
| Outcomes | 2.353 |
| Ganadores | 202 |
| Win rate | 8,584785% |
| Retorno medio | -4,557907% |
| Suma de retornos | -10.724,754332 puntos porcentuales |
| Profit factor | 0,483938 |
| PnL hipotético a 0,1 SOL, antes de costes | **-10,724754 SOL** |

Este resultado descarta una relajación general de filtros. Los 40 picos perdidos de al menos +100%, incluidos 6 de al menos +500% y 5 de al menos +1.000%, no cambian esa conclusión: un pico posterior no prueba que hubiera una ruta ejecutable, un fill al precio observado, una salida realizable ni rentabilidad neta. Varios candidatos de gran pico también tenían señales tóxicas, baja calidad o datos incompletos.

### Subconjunto base ejecutable

Exigir `dex_id=pumpswap`, ruta Jupiter y `cluster_bad=false` deja 10 outcomes:

| Métrica | Resultado |
|---|---:|
| Outcomes | 10 |
| Ganadores | 6 |
| Win rate | 60,00% |
| Retorno medio | +8,540999% |
| Profit factor | 1,579720 |
| PnL hipotético a 0,1 SOL, antes de costes | +0,085410 SOL |

La señal mejora de forma importante, pero diez casos siguen siendo insuficientes para promoción.

### Hipótesis selectiva de siete casos

Al añadir frescura, liquidez, market cap, actividad, score e impacto se obtuvieron siete outcomes:

| Día UTC | Símbolo | PnL shadow | Edad min | Cola min | Liquidez USD | Market cap USD | Txns 5m | Score | Impacto |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-07-13 | Fav glow | +6,8907% | 42,77 | 0,33 | 36.755 | 237.630 | 1.154 | 70 | 2,18% |
| 2026-07-13 | ANUKONG | +107,9614% | 35,02 | 7,72 | 30.806 | 168.629 | 1.050 | 70 | 8,54% |
| 2026-07-13 | BULLARC | -19,4348% | 16,45 | 7,71 | 26.180 | 122.237 | 283 | 50 | 4,71% |
| 2026-07-14 | TrumpCoin | +55,6229% | 11,21 | 0,01 | 23.108 | 97.757 | 1.014 | 60 | 0,00% |
| 2026-07-14 | trumpbrain | +29,6996% | 39,72 | 6,23 | 29.739 | 157.072 | 1.014 | 70 | 0,00% |
| 2026-07-14 | Ca$hcats | +13,3105% | 9,59 | 0,01 | 22.279 | 90.946 | 1.287 | 50 | 1,12% |
| 2026-07-14 | MAGACOIN | +19,2545% | 37,37 | 11,11 | 28.979 | 145.992 | 1.128 | 70 | 8,38% |

Resumen del subconjunto: 7 outcomes, 6 ganadores, win rate 85,714286%, retorno medio +30,472136%, profit factor 11,975412 y PnL hipotético agregado **+0,213305 SOL** a 0,1 SOL por operación antes de costes. Por día, el resultado fue +0,095417 SOL en tres casos del 13 de julio y +0,117888 SOL en cuatro casos del 14 de julio.

### Limitaciones que invalidan cualquier promesa de beneficio

- Es una regla **post-hoc** formulada después de observar los outcomes; existe riesgo alto de sobreajuste.
- Son solo siete casos y un único régimen temporal de aproximadamente 36 horas.
- Los siete valores históricos de `liquidity_is_proxy` quedaron `null`: no es posible demostrar que la liquidez usada fuera explícitamente real.
- Los outcomes son shadow, no fills reales. No incorporan fallos de ruta, latencia de firma, slippage realizado, priority fee, tips, coste de salida ni MEV.
- El event replay generó 0 compras simuladas y declaró `acceptance_ready=false`; por tanto, no valida el executor.
- El resultado está dominado parcialmente por `ANUKONG`; una observación extrema puede inflar la media y el profit factor.

La clasificación correcta es **hipótesis candidata a forward paper**, no estrategia ganadora demostrada.

## Calidad de datos

### Feature store

El feature store contiene 7.735 filas y todas están marcadas como incompletas. Coberturas observadas:

| Feature | Cobertura |
|---|---:|
| `txns_last_5m` | 85,3% |
| `price_pct_5m` | 77,0% |
| trend derivado | 66,9% |
| `holders` | 0% |
| `rug_score` | 0% |
| `social_ok` | 0% |
| `price_pct_1m` | 0% |
| `volume_pct_5m` | 0% |

La ausencia de holders, rug, social y micro-momentum impide tratar el dataset como una representación completa del riesgo. Las features con cobertura parcial sí sirven para diagnóstico y para una prueba paper acotada si cada compra exige explícitamente sus campos obligatorios.

### Social y contratos de telemetría

Las 2.037 filas de enriquecimiento social quedaron en `unknown`. Por ello, la política prospectiva no depende de social como señal positiva.

La serialización histórica convertía valores explícitos cero/falso —especialmente `liquidity_is_proxy=false` y confirmaciones de rebound— en ausencia de dato. Esto hacía imposible distinguir “confirmado como no proxy” de “no medido”. Se ha corregido el contrato para preservar cero y false, pero los datos ya escritos no se pueden reconstruir de forma fiable.

También había eventos con acción `buy`/`bought` sin evidencia de orden o ejecución. Esos registros no deben contar como compras. La normalización reparada conserva la intención original, pero los clasifica como decisión/observación salvo que exista evidencia de ejecución (`order_id` u otro marcador válido).

### Consistencia de fuentes

- SQLite y la evidencia de orden coinciden en 0 intentos, 0 compras y 0 posiciones.
- El decision ledger tiene 14 IDs duplicados sobre 15.991 filas; los análisis deduplican donde corresponde.
- `api_budget.json` informa cero llamadas pese a que los logs contienen actividad de proveedores. No se considera una fuente fiable para medir consumo o disponibilidad en este run.
- La base de datos mantenía `process_state=running` después de morir el PID. El estado de runtime no era fiable sin reconciliar PID, lock y heartbeat.

Conclusión de calidad: la evidencia es suficiente para demostrar la causa de cero compras y evaluar outcomes shadow, pero no para certificar liquidez real histórica, costes netos, execution quality o preparación live.

## Auditoría de logs y proveedores

No se encontró traceback fatal ni líneas `ERROR`/`CRITICAL` que expliquen la ausencia total de compras. La ejecución siguió evaluando candidatos.

| Señal | Conteo / resultado |
|---|---:|
| Birdeye HTTP 404 | aproximadamente 32.973 |
| Warnings por campos críticos null | aproximadamente 13.238 |
| Desconexiones PumpFun/PumpPortal | 8, todas recuperadas |
| Candidatos finales sin precio | 8 |
| Errores RugCheck | 2 |
| 429 confirmados | 0 |
| Tracebacks fatales | 0 |

Los 404 y nulls no tumbaron el proceso, pero degradaron la completitud y aumentaron evaluaciones inútiles. La cola PumpFun tenía además un defecto funcional: devolvía repetidamente un cache en lugar de consumir eventos una sola vez, y podía perder el excedente de lotes grandes. Esto no aparece como excepción, pero distorsiona oportunidad, edad y deduplicación.

## Modelos ML

### Comportamiento durante el run

- 4.928 decisiones ML tuvieron `passed=true` y `enforced=false`; ML no bloqueó las compras.
- Al arrancar no existía un `ml/model.pkl` activo válido.
- Un fallback de modelo candidato influyó más tarde en rank, pese a no estar aprobado para activación.
- El perfil corregido fija `ML_SHADOW_CANDIDATE_MODEL_FALLBACK_ENABLED=false` para que un candidato no promovido no cambie la política prospectiva.

### Último candidato entrenado

El artefacto `ml/models/20260715_000729_lightgbm_small` no es apto para activación:

| Métrica | Resultado |
|---|---:|
| Holdout total / positivos | 1.276 / 58 |
| Seleccionados | 63 |
| Precision | 0,333 |
| Floor requerido | 0,60 |
| PnL medio de seleccionados | -5,146% |
| PnL mediano de seleccionados | -21,384% |
| `activation_ready` | `false` |

El training partió de 7.671 filas y solo 2.129 fueron elegibles, con 86 positivos. No había cierres reales (`trade_close`) en el conjunto útil: predominan rejects y shadows. La ventana tampoco permite un split forward robusto. El modelo puede seguir usándose para investigación offline/shadow, pero no para bloquear, dimensionar ni promocionar capital.

## AutoResearch

Se auditaron 36 ciclos: 35 completados y 1 omitido. Generaron 105 candidatas, todas rechazadas; hubo 0 aceptadas, 0 fallidas y objetivo agregado 0.

El sistema estaba operando y generando propuestas, pero no encontró una candidata con evidencia causal suficiente. El event replay produjo 0 compras simuladas y más de 13.000 decisiones quedaron bloqueadas por routing/política; por eso no podía validar la ejecución ni estimar expectancy de fills.

El smoke aislado del launcher usa fixtures y puede producir candidatas “accepted” dentro de ese fixture. Ese resultado verifica el mecanismo, no es evidencia del run ni debe sumarse al scoreboard productivo.

Conclusión: AutoResearch no estaba roto por excepción, pero su prueba estaba desconectada de un camino de compra habilitado y sus datos no alcanzaban calidad de activación. La política nueva crea un camino paper explícito sin permitir promoción live automática.

## Política prospectiva implementada: 0,1 SOL exactos

Solo `paper_bootstrap` puede comprar durante la prueba. Una compra paper requiere simultáneamente:

| Gate | Requisito |
|---|---|
| Modo | `DRY_RUN=1`; live bloqueado |
| Tamaño | **exactamente 0,1 SOL** |
| DEX | `pumpswap` |
| Ruta | Jupiter route explícita |
| Liquidez | explícitamente no proxy |
| Cluster | `cluster_bad=false` |
| Edad del token | ≤60 minutos |
| Edad en cola | ≤15 minutos |
| Liquidez | ≥20.000 USD |
| Market cap | ≥50.000 USD |
| Transacciones 5m | ≥100 |
| Score total | ≥50 |
| Impacto de precio | ≤12% |

La excepción para `price_pct_5m` negativo solo se permite si el cambio es ≥-25%, hay al menos 1.000 transacciones en 5 minutos, market cap entre 90.000 y 250.000 USD, liquidez ≥20.000 USD e impacto ≤12%.

Controles de exposición:

- Máximo 2 posiciones abiertas.
- Máximo 6 compras por día.
- Máximo 2 compras por hora.
- Cooldown de 600 segundos.
- Ninguna reducción silenciosa del tamaño: si el control de riesgo no autoriza exactamente 0,1 SOL, se bloquea la compra.
- Bootstrap alternativo, shadow follow-up, exploración, green sniper, rank buys y lanes live permanecen desactivados.
- Auto-promoción, live execution y fallback de modelo candidato permanecen en `false`.

Esta política busca más oportunidades **válidas**, no más volumen indiscriminado. Si no aparece un candidato que cumpla todos los gates, el comportamiento correcto sigue siendo no comprar.

## Cambios implementados

### Flujo principal y colas

- `run_bot.py`: el monitor de posiciones `_check_positions()` se ejecuta una vez por ciclo principal; ya no depende de que exista un candidato legacy ni se repite dentro de ese bucle.
- `utils/lista_pares.py`: los candidatos incompletos expiran por un límite absoluto aunque estén en cooldown; se registra `queue_drop` con `incomplete_timeout`.
- `fetcher/pumpfun.py`: cola FIFO consumible, cada evento se entrega una sola vez, los lotes de más de 75 conservan el excedente para llamadas posteriores, y dedupe/TTL/overflow quedan controlados.
- `run_bot.py`: una oportunidad sin lane/tag ya no cae automáticamente en shadow; antes pasa por la política bootstrap estricta y solo continúa si todos sus gates prospectivos son válidos.

### Contratos de datos

- `analytics/research_runtime.py`: preserva `false` y cero explícitos para proxy de liquidez y confirmaciones de rebound.
- `ml/data_contract.py`: una acción declarada como live/buy sin evidencia de orden se normaliza como decisión abierta/observación, manteniendo `raw_action` e intención para auditoría.
- `features/decision_store.py`: usa el mismo contrato canónico, evitando divergencias entre training, research y runtime.
- Los tres campos ausentes observados en los siete ejemplos retrospectivos (`holders`, `rug` y `social`) son opcionales y diagnósticos; el gate runtime cuenta por separado los obligatorios `price`, `liquidity`, `market_cap` y `txns`. Por eso se conserva `PAPER_BOOTSTRAP_MAX_SNAPSHOT_MISSING_FIELDS=1` y no se relaja a tres.

### Política y preflight

- `analytics/paper_bootstrap.py`: incorpora DEX, ruta, no-proxy, frescura y tamaño exacto; bloquea un sizing inferior cuando la política exige 0,1 SOL.
- La misma política exige ahora observaciones explícitas: proxy de liquidez y clúster desconocidos, impacto ausente, momentum 5m ausente o edad de cola ausente bloquean de forma trazable. Los valores explícitos `false` y `0` siguen siendo válidos.
- `config/config.py`, `.env` y `.env.example`: añaden y documentan los nuevos gates sin romper la paridad de claves.
- `config/profiles/paper_hotfix_0707.env`: configura la política selectiva, sus límites y la desactivación de todas las vías competidoras/live.
- `tools/preflight.py`: falla cerrado si bootstrap, Pumpswap o tamaño exacto no están activos, si alguna ruta alternativa/live está habilitada, o si cantidades/caps no son 0,1 SOL.

### Lifecycle

- `scripts/start_stack.ps1`: antes de borrar un lock obsoleto exige reconciliar de forma estricta el estado final en SQLite; si la reconciliación falla, conserva el lock y aborta.
- `scripts/finalize_stack_stop.py`: el modo `--require-runtime-state` requiere base, tabla y fila exacta, devuelve error JSON y exit code 1 si no puede certificar la transición.
- `scripts/stop_stack.ps1`: la parada normal revalida PID, command line, fecha de creación y marcadores del repo antes de escalar a `/F`; no mata procesos ajenos y deja `reports_refresh_state`/`retrain_state` en `idle`.

### Arranque y coste analítico

- El balance paper inicial se consulta en background y el backfill de `paper_portfolio` no toca CoinGecko si no existen posiciones que reparar. Esto elimina una espera observada de 119 segundos sin trabajo útil.
- La reparación SQL de notionals consulta primero si existen filas y solo solicita SOL/USD cuando hay un notional positivo realmente pendiente.
- Los informes core frescos no se regeneran de nuevo al arrancar. Cuando toca regenerarlos, se ejecutan en un subproceso de baja prioridad para no retener el GIL del bot.
- La señal de recuperación ya no parsea los 134,7 MB completos de `candidate_outcomes.jsonl`: lee una cola acotada, expone `scan_truncated` y falla cerrada si esa ventana no aporta evidencia suficiente.

### Auditoría reproducible

- `analytics/causal_shadow_policy.py`: reconstruye la unión causal y las métricas de los universos de decisión.
- `tools/causal_shadow_policy_audit.py`: genera el informe JSON reproducible.
- `tests/test_causal_shadow_policy.py`: cubre selección, unión temporal y métricas.
- Evidencia generada: `data/metrics/causal_shadow_policy_audit_20260713-1.json`.

## Validación técnica

| Validación | Resultado |
|---|---|
| Suite completa final | **783 passed en 45,20 s** |
| Tests focales de los últimos contratos de arranque/política | 34 passed |
| Ruff sobre archivos nuevos/tocados | `All checks passed!` |
| Ruff global | 233 avisos preexistentes; no se atribuyen a este cambio |
| Config efectiva con perfil | `DRY_RUN=True`, bootstrap activo, amount/max/cap 0,1, fallback ML falso |
| Candidato sintético válido | permitido por 0,1 SOL |
| Candidato stale | bloqueado por edad |
| Preflight real | `status=ok`, `errors=[]`, 99 variables de perfil |
| Strategy quality gate | `ok` |
| AutoResearch smoke aislado | `status=ok`, 3 fixtures, `live_remains_false=true` |

### Ciclo real posterior

El preflight oficial completo del launcher se ejecutó satisfactoriamente en una pasada previa. Tras las últimas correcciones se repitió un humo rápido con `scripts/start_stack.ps1 -IncludeBot -SkipStartupPreflight -AutoResearchSkipRegenerateReports`; se omitió únicamente el preflight redundante porque la suite completa acababa de pasar.

- Run de validación final: `20260715-8`, `DRY_RUN=true`, perfil `paper_hotfix_0707`.
- `process_state=running`, API y UI HTTP 200, `last_error=null`.
- Primer descubrimiento aproximadamente un segundo después del boot audit; 13 candidatos raw en el primer lote.
- Cinco evaluaciones `paper_bootstrap_eval`, todas con `amount_sol=0.1`; una llegó desde `entry_lane_guard` y cuatro desde `basic_filter`.
- Cero `buy_attempt`, `order_attempt`, `buy_executed` o posiciones. No se forzó una compra sintética contra datos malos.
- `monitor_last_ok_at=2026-07-15T18:18:25Z`, con reports y retrain en `idle`.
- Parada normal final: `remaining=0`, `data/run_bot.lock` ausente, API/UI abajo y SQLite `stopped/idle/idle`.
- La comprobación independiente posterior, excluyendo únicamente el propio proceso diagnóstico y sus ancestros, dio `stack_processes=0`.

## Decisión operativa

### Ahora

- **Paper: GO controlado** para la política selectiva de 0,1 SOL.
- **Live: NO-GO**.
- **Modelo como decisor: NO-GO**.
- **Auto-promoción: NO-GO**.

### Criterios mínimos de aceptación forward

La política no debe considerarse ganadora hasta cumplir, en datos nuevos que no hayan participado en su diseño:

1. Todas las entradas con `liquidity_is_proxy=false` explícito, ruta real y snapshot completo en los campos obligatorios.
2. Número suficiente de cierres paper para que el resultado no dependa de uno o dos outliers; informar intervalos de incertidumbre, no solo promedio.
3. PnL neto positivo después de slippage, priority fee, tips, costes de entrada/salida y fallos de fill simulados de forma conservadora.
4. Profit factor >1, expectancy >0 y drawdown dentro del presupuesto en varias ventanas/días y regímenes de mercado.
5. Sin regresión de pérdidas severas, liquidity crush, stale queue, duplicados o degradación crítica de proveedores.
6. Event replay con compras y cierres causales, `acceptance_ready=true` y paridad entre replay y policy runtime.
7. Modelo con `activation_ready=true`, precision ≥0,60 y evaluación forward suficiente; hasta entonces seguirá shadow y no afectará capital.
8. Canary live requeriría una autorización manual posterior, límites aún más bajos, kill switch y rollback verificados. Esta auditoría no concede esa autorización.

Si la ventana forward resulta negativa o no alcanza muestra/calidad, se debe rechazar la hipótesis y volver a research; no relajar gates para forzar operaciones.

## Fuentes y metodología

Fuentes primarias auditadas:

- `data/memebotdatabase.db`: posiciones, trades y estado runtime.
- `data/metrics/runtime_events.jsonl`: discovery, decisiones, bloqueos, configuración y lifecycle.
- `data/metrics/decision_ledger.jsonl`: snapshots predecisión y stages.
- `data/metrics/candidate_outcomes.normalized.jsonl`: outcomes terminales normalizados.
- `data/features/`: feature store y cobertura.
- `data/research_runs/`: ciclos, scoreboards y propuestas de AutoResearch.
- `ml/models/20260715_000729_lightgbm_small/`: candidato y métricas de validación.
- `logs/`: proveedores, errores, desconexiones y arranque.
- `data/metrics/causal_shadow_policy_audit_20260713-1.json`: reconstrucción causal reproducible.

Método:

1. Se congeló el `run_id` y se separaron los eventos de validación posterior.
2. Se reconciliaron SQLite, runtime events, ledger, outcomes, features, modelos, research y logs.
3. Las compras se contaron solo con evidencia de ejecución; una intención `buy` no equivale a una orden.
4. Cada outcome shadow se unió al último snapshot causal anterior a su apertura.
5. El PnL hipotético a tamaño fijo se calculó como `0,1 SOL × retorno_pct / 100` por outcome y se sumó; no incluye costes.
6. Se comparó el universo completo con filtros ejecutables y una hipótesis estricta; la última se etiqueta post-hoc y no acceptance-grade.
7. Se implementaron gates prospectivos, se ejecutaron tests y se validó el launcher real en paper.

## Conclusión

El run no compró por una configuración que cerraba todos los caminos de compra antes del executor, no por falta de descubrimiento ni por un error de orden. Abrir el funnel completo habría sido económicamente destructivo según los propios outcomes shadow. La corrección habilita una sola política paper de 0,1 SOL, basada en señales ejecutables y frescas, con límites de exposición y sin interferencia de modelos no aprobados.

La implementación mejora el aprovechamiento de oportunidades y la fiabilidad del sistema, pero **no convierte todavía a MemeBot3 en un bot ganador demostrado**. La única afirmación responsable es: código y gates listos para una prueba forward paper; live bloqueado hasta que la evidencia nueva, neta y ejecutable supere los criterios anteriores.
