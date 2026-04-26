# PLANPR

## Objetivo

Subir el PnL del bot sin romper el flujo actual ni introducir regresiones operativas.

La estrategia del plan es:

1. Corregir primero la medicion y la calidad de datos.
2. Endurecer despues la seleccion de entradas.
3. Optimizar ejecucion y exits sobre una base ya fiable.
4. Reentrenar y recalibrar solo cuando el dataset sea util.
5. Desplegar cada cambio por fases, con flags, validacion y rollback.

Este documento esta pensado para ejecutar la mejora completa por pasos, manteniendo el bot funcionando durante todo el proceso.

---

## Principios de implementacion

### Restricciones duras

- No romper el modo actual `DRY_RUN`.
- No romper el modo real.
- No cambiar de golpe la estrategia en un unico commit.
- No mezclar cambios de medicion con cambios de edge.
- No activar un cambio sin observabilidad suficiente.
- No eliminar comportamiento actual sin tener un reemplazo compatible.

### Reglas de seguridad

- Todo cambio sensible debe ir detras de flags o mantener backward compatibility.
- Toda nueva columna en DB debe migrarse de forma segura y condicional.
- Todo cambio en dataset debe poder convivir con los parquet actuales.
- Toda mejora de filtros debe activarse primero en shadow o paper.
- Todo cambio de threshold debe justificarse con datos.

### Orden obligatorio

1. Baseline y guardrails.
2. Correccion de PnL y etiquetas.
3. Integridad de features y fetchers.
4. Observabilidad y reporting.
5. Endurecimiento de filtros y sizing.
6. Optimizacion de exits.
7. ML y calibracion por PnL.
8. Rollout gradual a real.

---

## Resumen ejecutivo del diagnostico actual

### Hallazgos principales

1. El bot compra casi todo lo que consigue evaluar.
   - Config efectiva observada:
     - `AI_THRESHOLD=0.0`
     - `BUY_SOFT_SCORE_MIN=0`
     - `MIN_HOLDERS=0`
     - `MIN_AGE_MIN=0.2`
     - `MAX_MARKET_CAP_USD=8000000`
     - `REQUIRE_JUPITER_FOR_BUY=False`
   - Esto reduce casi a cero la selectividad real.

2. El PnL y las etiquetas de trades con parcial estan mal medidos.
   - Se esta valorando el trade final por el precio del remanente.
   - No se incorpora de forma correcta lo ya realizado en la venta parcial.
   - Esto contamina:
     - `close_price_usd`
     - `pnl_pct`
     - `outcome`
     - labels del parquet
     - futuros entrenamientos

3. Hay incoherencia entre `WIN_PCT` y `TAKE_PROFIT_PCT`.
   - Hay dos fuentes de verdad en config.
   - El labeler, el runner y paper trading no usan exactamente la misma definicion.

4. El feature store actual no sirve todavia para extraer edge de ML.
   - Muy pocas filas.
   - No hay modelo entrenado presente.
   - Muchas columnas estan constantes o vacias.
   - `txns_last_5m`, `holders`, `rug_score`, `trend`, `social_ok` aparecen sin poder discriminante.

5. Se estan eliminando o degradando features validas de T0.
   - `price_service` elimina claves `txns_last_*`, incluyendo `txns_last_5m`, que si es una feature valida de entrada.
   - Varias ausencias se convierten en `0`, perdiendo informacion sobre missingness.

6. Hay bugs tecnicos que no necesariamente hunden el bot, pero si degradan calidad o trazabilidad.
   - Contrato inconsistente de cache en `analytics/trend.py`.
   - Migracion SQLite mirando tabla `position` en vez de `positions`.
   - DRY_RUN no replica exactamente la logica real de parciales.

7. El bot requeuea muchisimo y convierte muy pocos requeues en oportunidades validas.
   - Hay mucho coste operativo y ruido de discovery/fetch.

### Conclusiones

- La prioridad numero uno no es tocar trailing ni inventar un modelo nuevo.
- La prioridad numero uno es hacer fiable la medicion del edge.
- La prioridad numero dos es reducir el ruido de entrada.
- La prioridad numero tres es optimizar el uso de ganadores y limitar colapsos de liquidez.

---

## Objetivos del proyecto de mejora

### Objetivos primarios

- Mejorar PnL neto por trade.
- Mejorar expectancy del sistema.
- Reducir drawdowns provocados por sobreentrada.
- Reducir trades claramente malos antes de comprar.
- Medir correctamente el rendimiento real de parciales.
- Construir un dataset util para decisiones futuras.

### Objetivos secundarios

- Reducir requeues inutiles.
- Mejorar trazabilidad de fuentes de precio y liquidez.
- Separar mejor regimens de mercado.
- Preparar el proyecto para tuning y ML de verdad.

### KPIs target

Los targets exactos se revisaran con mas muestra, pero el plan debe orientarse a:

- Menos compras por dia, pero con mas EV por compra.
- Menor porcentaje de `STOP_LOSS` y `LIQUIDITY_CRUSH`.
- Menor giveback medio desde `highest_pnl_pct` al cierre.
- Mayor precision en top trades aceptados.
- Labels y outcomes coherentes con PnL total realizado.

---

## Enfoque de despliegue

### Estrategia de rollout

- Fase A: cambios de exactitud sin cambiar estrategia.
- Fase B: cambios de observabilidad y reporting.
- Fase C: cambios de filtros detras de flags.
- Fase D: cambios de exits detras de flags.
- Fase E: calibracion y ML.
- Fase F: activacion gradual en paper, shadow y real.

### Politica de activacion

- Todo cambio nuevo empieza:
  - apagado por defecto, o
  - replicando comportamiento actual por defecto.
- Se valida primero en:
  - tests unitarios
  - smoke local
  - `DRY_RUN`
  - `REAL_SHADOW_SIM`
  - luego real con canary

### Politica de rollback

- Ninguna fase debe impedir volver al comportamiento anterior solo con config.
- Si una fase no cumple criterios de aceptacion, se congela y no se encadena la siguiente.

---

## Fase 0 - Baseline, guardrails y preparacion

### Objetivo

Congelar una referencia fiable del estado actual para poder comparar despues.

### Trabajo

1. Crear una baseline de configuracion efectiva.
   - Exportar todos los valores relevantes de `CFG`.
   - Guardar snapshot de `.env` en formato seguro sin secretos, o al menos un diff de parametros operativos.

2. Sacar baseline de rendimiento actual.
   - Numero total de trades.
   - Trades cerrados.
   - Win rate simple.
   - PnL medio.
   - PnL mediano.
   - Breakdown por `exit_reason`.
   - Breakdown por `partial_taken`.
   - Drawdown simple por secuencia.
   - Tiempo medio de hold.
   - Giveback medio desde peak.

3. Sacar baseline del dataset.
   - Numero de filas.
   - Numero de positivos.
   - Nulos por columna.
   - Columnas constantes.
   - Numero de tokens unicos.

4. Crear herramientas de chequeo rapido.
   - Script de auditoria de DB.
   - Script de auditoria de parquet.
   - Script de sanity de config.

5. Documentar el procedimiento de rollback.

### Archivos implicados

- `run_bot.py`
- `config/config.py`
- `data/memebotdatabase.db`
- `data/features/*.parquet`
- `docs/PLANPR.md`
- nuevo modulo utilitario si hace falta, por ejemplo `analytics/reporting.py` o `scripts/`.

### Entregables

- Script baseline DB.
- Script baseline parquet.
- Documento corto con output baseline.

### Riesgo

- Muy bajo.

### Criterio de aceptacion

- Podemos medir el antes y el despues de cada fase.

---

## Fase 1 - Corregir PnL real, parciales y etiquetas

### Objetivo

Hacer que el bot mida correctamente el resultado economico real de cada trade.

### Problema actual

- El parcial se ejecuta, pero el cierre final del trade ignora lo realizado previamente.
- `close_price_usd` del remanente se usa como si fuera el precio de toda la posicion.
- `labeler` y el persistido del dataset usan esa version incompleta.

### Impacto esperado

- Muy alto.
- Sin esta fase, cualquier optimizacion posterior estara sesgada.

### Trabajo

#### 1. Introducir contabilidad de trade total

Agregar soporte para:

- `entry_qty`
- `remaining_qty`
- `realized_qty`
- `realized_proceeds_usd`
- `realized_cost_usd`
- `realized_pnl_usd`
- `realized_pnl_pct`
- `unrealized_pnl_usd`
- `total_pnl_usd`
- `total_pnl_pct`
- `partial_count`
- `last_partial_price_usd`
- `last_partial_qty`
- `first_partial_at`
- `last_partial_at`

No hace falta usar exactamente estos nombres, pero la semantica debe existir.

#### 2. Definir claramente el modelo economico del trade

Para un trade con parcial:

- PnL realizado parcial:
  - sobre la fraccion vendida al precio parcial.
- PnL del remanente:
  - sobre la fraccion que queda y luego se cierra.
- PnL total:
  - suma ponderada de ambos tramos.

#### 3. Corregir persistencia en modo real

En `run_bot.py`:

- Al hacer parcial:
  - registrar cantidad vendida
  - registrar precio parcial usado
  - acumular realized PnL
  - no dejar el trade en estado ambiguo
- Al cierre final:
  - calcular `total_pnl_pct`, no solo el del remanente
  - guardar resultado final coherente

#### 4. Corregir persistencia en `papertrading`

Hoy paper trading replica mal esta parte.

Debe:

- almacenar correctamente la parcial
- calcular `pnl_pct` final total del trade
- no sobreescribir resultado con solo el remanente

#### 5. Corregir etiquetado

Actualizar:

- `labeler/win_labeler.py`
- persistencia de dataset al cierre en `run_bot.py`

Las etiquetas deben basarse en:

- `total_pnl_pct` o equivalente economico total,
- no en `close_price_usd` final del remanente.

#### 6. Unificar semantica de win

Definir una sola fuente de verdad:

- o `WIN_PCT`
- o `TAKE_PROFIT_PCT`

Pero no ambas con semantica paralela.

Recomendacion:

- dejar `TAKE_PROFIT_PCT` como parametro de salida
- derivar `WIN_PCT` de ahi solo para etiquetado
- usar una sola funcion helper comun para resolver ambos

#### 7. Compatibilidad y migracion

Si se anaden columnas en `positions`:

- migracion condicional
- backward compatible
- no asumir tabla incorrecta

### Archivos implicados

- `run_bot.py`
- `trader/papertrading.py`
- `labeler/win_labeler.py`
- `db/models.py`
- `db/database.py`
- `config/exits.py`
- `config/config.py`

### Validacion

- Tests con trade sin parcial.
- Tests con un parcial y cierre final.
- Tests con varios parciales si se quiere soportar mas adelante.
- Test de label `win/fail` para:
  - trade sin parcial
  - trade con parcial ganador y remanente malo
  - trade con parcial mediocre y remanente ganador

### Criterio de aceptacion

- El PnL final de un trade con parcial coincide con el calculo economico total esperado.
- `outcome`, `label` y reporting usan esa misma verdad.

### Rollback

- Mantener campos viejos durante una fase de compatibilidad.
- Si algo falla, seguir leyendo los campos antiguos y desactivar el nuevo calculo con flag.

---

## Fase 2 - Integridad de datos T0 y recuperacion de features

### Objetivo

Recuperar features que hoy se pierden o se degradan antes de llegar al dataset.

### Problemas detectados

- Se eliminan claves `txns_last_*`, incluyendo `txns_last_5m`.
- Muchos missing se transforman en `0`, destruyendo informacion.
- Varias features quedan constantes.
- `trend` tiene contrato inconsistente en cache.

### Trabajo

#### 1. Corregir el saneado de claves no-T0

En `utils/price_service.py`:

- dejar de eliminar ciegamente `txns_last_*`
- solo eliminar claves realmente prohibidas de futuro
- preservar:
  - `txns_last_5m`
  - `txns_last_5m_sells`
  - cualquier feature de snapshot valida

#### 2. Separar missing de cero real

Hoy muchas ausencias acaban como `0`.

Cambiar estrategia:

- conservar `None`/`NaN` hasta el punto en que sea necesario
- anadir flags tipo:
  - `missing_liquidity`
  - `missing_volume`
  - `missing_holders`
  - `missing_rug_score`
  - `missing_socials`
  - `missing_trend`

#### 3. Revisar `sanitize_token_data`

No debe:

- aplastar datos demasiado pronto
- convertir todo a ceros si todavia estamos en T0

Debe:

- normalizar tipos
- preservar missingness informativa
- seguir siendo seguro para DB y filtros

#### 4. Corregir `analytics/trend.py`

Actualmente el cache guarda `sig`, pero la funcion devuelve tuple.

Hay que unificar el contrato:

- o siempre `(sig, fallback_used)`
- o cachear el tuple completo

#### 5. Revisar fetchers para enriquecer features utiles

Analizar y recuperar de forma fiable:

- `holders`
- `txns_last_5m`
- `rug_score`
- `social_ok`
- `trend`
- posible `dexId` fiable
- liquidez y volumen con fuente/freshness

#### 6. Guardar provenance y freshness

Anadir si compensa:

- `price_source`
- `liquidity_source`
- `volume_source`
- `price_age_s`
- `liquidity_age_s`

No necesariamente para el modelo final, pero si para auditoria.

#### 7. Auditar columnas constantes

No entrenar con features sin variacion.

### Archivos implicados

- `utils/price_service.py`
- `utils/data_utils.py`
- `analytics/trend.py`
- `fetcher/dexscreener.py`
- `fetcher/birdeye.py`
- `fetcher/socials.py`
- `fetcher/rugcheck.py`
- `features/builder.py`
- `features/store.py`

### Validacion

- Test unitario para asegurar que `txns_last_5m` no se elimina.
- Test de builder con missings.
- Test de trend cache contract.
- Re-auditoria del parquet tras unas horas de paper.

### Criterio de aceptacion

- El nuevo parquet contiene variacion real en mas columnas.
- Las columnas clave ya no son todas constantes o cero.

---

## Fase 3 - Observabilidad operativa y reporting de edge

### Objetivo

Poder saber exactamente donde gana y donde pierde el bot.

### Trabajo

#### 1. Reporting por salida

Crear reporte periodico con:

- conteo por `exit_reason`
- PnL medio y mediano por `exit_reason`
- PnL total por `exit_reason`
- giveback medio por `exit_reason`

#### 2. Reporting por regimen

Separar estadisticas por:

- `discovered_via`
- `dex_id`
- bucket de edad
- bucket de liquidez
- bucket de market cap
- bucket de score

#### 3. Reporting por fuente de precio

Comparar:

- `price_source_at_buy`
- `price_source_at_close`

Buscar si ciertos origenes de precio se correlacionan con peores resultados.

#### 4. Reporting de cobertura de datos

Metricas:

- `% trades con holders`
- `% trades con txns_5m`
- `% trades con rug_score`
- `% trades con socials`
- `% trades con trend`

#### 5. Reporting de requeues

Medir:

- requeues por razon
- conversion de requeue a buy
- tiempo medio desde first_seen hasta buy

#### 6. Reporting de parciales

Medir:

- parcial tomado si/no
- PnL total con parcial vs sin parcial
- giveback con parcial vs sin parcial
- parcial ganador seguido de cierre perdedor

### Archivos implicados

- nuevo modulo, por ejemplo `analytics/reporting.py`
- `run_bot.py`
- `utils/logger.py`
- scripts de auditoria

### Criterio de aceptacion

- Podemos responder con datos a:
  - que tipo de trade pierde mas
  - que filtros sobran
  - que exits mejoran PnL

---

## Fase 4 - Endurecer entrada sin romper el bot

### Objetivo

Reducir sobretrading y filtrar mejor el ruido.

### Estrategia

No cambiar todo de golpe. Hacerlo por capas y por flags.

### Trabajo

#### 1. Reintroducir gates minimos

Subir gradualmente:

- `MIN_AGE_MIN`
- `MIN_HOLDERS`
- `BUY_SOFT_SCORE_MIN`
- `AI_THRESHOLD`

Recomendacion inicial de paper:

- `MIN_AGE_MIN`: 2 a 3 min
- `MIN_HOLDERS`: 15 a 25
- `BUY_SOFT_SCORE_MIN`: 45 a 55
- `AI_THRESHOLD`: seguir a 0 mientras no haya modelo, pero en cuanto exista modelo real, usar threshold calibrado

#### 2. Reducir universo

Revisar:

- `MAX_MARKET_CAP_USD`
- `MIN_LIQUIDITY_USD`
- `MIN_VOL_USD_24H`

La ventana de `MAX_MARKET_CAP_USD=8,000,000` es demasiado amplia para este bot.

Debe acotarse por regimen, no de forma global absurda.

#### 3. Requerir ejecutabilidad minima

Volver a considerar:

- `REQUIRE_JUPITER_FOR_BUY=true`

pero primero en paper/shadow.

#### 4. Introducir filtros por calidad de snapshot

No comprar si faltan demasiadas piezas criticas de informacion.

Ejemplo:

- si no hay liquidez fiable
- si no hay volumen fiable
- si la price source es demasiado debil

#### 5. Desacoplar filtros por descubrimiento

No usar el mismo perfil para:

- `pumpfun`
- `dex`
- futuros `revival`

### Archivos implicados

- `config/config.py`
- `analytics/filters.py`
- `run_bot.py`
- `.env.example`

### Validacion

- A/B en paper.
- Shadow comparando:
  - bot actual
  - bot endurecido

### KPI esperado

- Menos `bought`.
- Mejor PnL medio.
- Menor ratio de `STOP_LOSS`.

---

## Fase 5 - Separacion por regimen de estrategia

### Objetivo

Evitar que un solo set de thresholds intente operar todos los contextos.

### Regimenes a introducir

1. `pump_early`
   - tokens muy recientes
   - alta incertidumbre
   - exige mas control de liquidez y route

2. `dex_mature`
   - pares ya visibles y algo mas estabilizados
   - mejor calidad de datos

3. `revival`
   - pools reactivados
   - edge distinto

### Trabajo

#### 1. Definir clasificador de regimen

Basado en:

- `discovered_via`
- `age_minutes`
- si hubo requeue y cuantas veces
- disponibilidad de liquidez/volumen

#### 2. Parametrizar por regimen

Cada regimen debe poder tener:

- `min_liquidity`
- `min_volume`
- `max_market_cap`
- `min_holders`
- `buy_soft_score_min`
- `tp_pct`
- `sl_pct`
- `trailing_pct`
- `no_pump`
- `time_stop`
- sizing maximo

#### 3. Mantener default backward compatible

Si no se define regimen, usar comportamiento actual.

### Archivos implicados

- `run_bot.py`
- `analytics/filters.py`
- `config/config.py`
- posiblemente nuevo `config/regimes.py`

### Validacion

- Reportes separados por regimen.
- Confirmar que un regimen no empeora el otro.

---

## Fase 6 - Optimizacion de sizing y gestion de riesgo en entrada

### Objetivo

No apostar lo mismo a setups de distinta calidad.

### Trabajo

#### 1. Introducir dynamic position sizing

Sizing por score y calidad del setup.

Inputs posibles:

- liquidez
- market cap
- score_total
- probabilidad ML
- route/impact
- regimen

#### 2. Reglas de sizing conservadoras

Ejemplo:

- setups marginales: 0.25x size
- setups aceptables: 0.5x size
- setups premium: 1.0x size

#### 3. Limitar exposicion por correlacion

No tener demasiados tokens del mismo perfil al mismo tiempo.

#### 4. Revisar `MAX_ACTIVE_POSITIONS`

Con entrada mas selectiva, probablemente conviene bajar posiciones activas.

#### 5. Revisar `BUY_RATE_LIMIT`

No debe ser tan amplio si el bot tiende a agruparse en bursts de ruido.

### Archivos implicados

- `run_bot.py`
- `config/config.py`

### Validacion

- Simular sizing sobre historico.
- Medir PnL por unidad de riesgo.

---

## Fase 7 - Optimizacion de exits sin destruir el edge

### Objetivo

Reducir giveback y mejorar monetizacion de ganadores.

### Datos actuales relevantes

- `TRAILING_STOP` parece capturar gran parte de los winners.
- `STOP_LOSS` agrega varias perdidas controladas.
- `LIQUIDITY_CRUSH` es especialmente destructivo.
- Hay trades con parcial ganador y cierre final negativo.

### Trabajo

#### 1. Revisar logica de `POST_PARTIAL_STOP`

Con medicion correcta del trade total, reevaluar:

- stop en breakeven
- stop en pequeno profit
- trailing especifico post-parcial

#### 2. Introducir trailing por regimen

No todos los setups deben tener el mismo trailing.

#### 3. Recalibrar parcial

Optimizar:

- `TP_PARTIAL_FRACTION`
- nivel donde se toma parcial
- si el parcial debe activarse una sola vez

#### 4. Mejorar proteccion contra colapso de liquidez

No solo cerrar por `LIQUIDITY_CRUSH`, tambien intentar evitar entrada si el riesgo es alto.

#### 5. Revisar `NO_PUMP` y `TIME_STOP`

Muy utiles para cortar trades muertos.

Deben calibrarse por regimen y no como una constante global ciega.

#### 6. Medir giveback de winners

Crear reporte:

- `highest_pnl_pct`
- `close_pnl_pct`
- diferencia

### Archivos implicados

- `run_bot.py`
- `trader/seller.py`
- `trader/papertrading.py`
- `config/exits.py`
- `config/config.py`

### Validacion

- Comparativa antes/despues de:
  - PnL medio
  - mediana
  - giveback
  - winners convertidos en losers tras parcial

---

## Fase 8 - Rehabilitar el pipeline ML correctamente

### Objetivo

Pasar de un pseudo-gate ML a un modelo realmente util.

### Condiciones previas obligatorias

No empezar esta fase hasta que:

- PnL de parciales este corregido.
- Labels sean correctas.
- Features no esten rotas.
- Haya suficiente muestra.

### Trabajo

#### 1. Definir umbral minimo de dataset

No entrenar con 24 filas.

Objetivo minimo operativo:

- cientos de filas utiles
- suficiente numero de positivos
- suficientes tokens unicos
- features no constantes

#### 2. Redefinir objetivo del threshold

No optimizar por F1 como objetivo final del bot.

La decision de compra debe alinearse con:

- expectancy
- precision en top picks
- PnL estimado por threshold

#### 3. Mejorar el target

Si el target sigue siendo binario:

- debe basarse en `total_pnl_pct`
- no en remanente parcial

Opcionalmente, valorar:

- target multinivel
- target de EV
- regression de retorno

#### 4. Enriquecer features

Solo despues de arreglar integridad:

- missing flags
- coverage flags
- requeue-derived features
- regime
- source quality
- impact/route availability

#### 5. Validacion temporal estricta

Mantener split forward temporal.

Adicionalmente:

- revisar leakage por token
- revisar leakage por requeue
- revisar leakage por features de cierre

#### 6. Calibracion por PnL

El threshold recomendado debe salir de:

- precision floor
- EV esperado
- o simulacion PnL sobre holdout

No solo de F1.

#### 7. Shadow model antes de activacion

El modelo nuevo debe correr en sombra antes de gate real.

### Archivos implicados

- `ml/train.py`
- `ml/retrain.py`
- `ml/tune_threshold.py`
- `analytics/ai_predict.py`
- `features/builder.py`
- `features/store.py`

### Validacion

- Holdout forward.
- Precision@K.
- EV estimado.
- Comparativa contra baseline heuristico.

### Criterio de aceptacion

- El modelo supera al baseline de reglas en top picks o EV.

---

## Fase 9 - Mejoras tecnicas de robustez

### Objetivo

Eliminar fallos tecnicos que sesgan operaciones o mediciones.

### Trabajo

1. Corregir migracion SQLite:
   - revisar tabla `positions` en vez de `position`.

2. Corregir contrato de `trend_signal`.

3. Alinear `papertrading` con modo real:
   - parciales
   - reason codes
   - PnL total

4. Añadir tests donde hoy no existen.

5. Añadir smoke checks de import y de integridad de config.

### Archivos implicados

- `db/database.py`
- `analytics/trend.py`
- `trader/papertrading.py`
- `tests/`

### Criterio de aceptacion

- Menos deuda operativa.
- Mayor confianza para tocar estrategia.

---

## Fase 10 - Rollout controlado

### Objetivo

Activar mejoras gradualmente sin cortar el bot.

### Etapas

#### Etapa 1 - Exactitud sin edge nuevo

Activar:

- PnL correcto
- labels correctas
- reporting nuevo

Sin endurecer filtros todavia.

#### Etapa 2 - Paper con filtros endurecidos

Activar:

- selectividad minima
- route requirement
- mejor control de snapshot

Comparar contra baseline.

#### Etapa 3 - Shadow en real

El bot real sigue igual, pero se registran:

- compras que haria la nueva estrategia
- diferencias de PnL esperado

#### Etapa 4 - Canary real

Activar nueva estrategia solo para:

- una parte del tiempo
- un subset de trades
- o size reducido

#### Etapa 5 - Full rollout

Solo si:

- PnL mejora
- drawdown no empeora materialmente
- calidad de labels confirmada

---

## Orden recomendado de PRs

### PR 1 - Baseline y reporting

Contenido:

- scripts baseline
- reportes
- metricas
- cero cambio de estrategia

### PR 2 - PnL total y parciales correctos

Contenido:

- DB
- run_bot
- papertrading
- labeler
- config semantica de win

### PR 3 - Integridad de features T0

Contenido:

- price_service
- data_utils
- trend
- builder/store

### PR 4 - Observabilidad de edge y requeues

Contenido:

- reportes por regime, exit, source

### PR 5 - Filtros endurecidos con flags

Contenido:

- nuevos thresholds
- activacion gradual

### PR 6 - Regimenes y sizing

Contenido:

- split por estrategias
- sizing dinamico

### PR 7 - Exits optimizados

Contenido:

- parcial
- stop post parcial
- trailing por regime
- no_pump / time_stop calibrados

### PR 8 - ML util y threshold por PnL

Contenido:

- retrain
- tune threshold
- holdout
- shadow activation

---

## Checklist de no regresion

Antes de mergear cualquier fase:

- El bot arranca.
- `DRY_RUN` arranca.
- Se cargan config y DB.
- No se rompe el discovery loop.
- No se rompe el monitor de posiciones.
- No se rompe el cierre de trades.
- No se rompe el parquet.
- No se rompe el labeler.
- Las nuevas columnas se migran de forma segura.
- Hay test o smoke check del cambio introducido.

---

## Checklist de validacion por fase

### Para fases de exactitud

- El numero de trades no cambia salvo que sea esperado.
- Cambia la medicion, no la operativa.
- Los trades historicos recalculados son coherentes.

### Para fases de filtros

- Bajan las compras.
- Sube la precision.
- No suben de forma fuerte los missed winners.

### Para fases de exits

- Baja el giveback medio.
- No se destruyen winners grandes.

### Para fases ML

- No activar si no supera baseline heuristico.

---

## Riesgos principales y mitigacion

### Riesgo 1 - Cambiar demasiadas cosas a la vez

Mitigacion:

- PRs pequeñas.
- Flags.
- rollout por fases.

### Riesgo 2 - Corromper labels historicas

Mitigacion:

- recalculo controlado
- backup de DB y parquet
- no sobreescribir sin snapshot

### Riesgo 3 - Endurecer demasiado y matar el flujo

Mitigacion:

- activacion gradual
- shadow comparison
- thresholds por regimen

### Riesgo 4 - Introducir migraciones peligrosas

Mitigacion:

- migraciones condicionales
- smoke local
- no eliminar columnas antiguas al principio

### Riesgo 5 - Sobreoptimizar con poca muestra

Mitigacion:

- no reentrenar hasta tener masa critica
- usar primero reglas y reporting

---

## Parametros a revisar primero en config

Estos parametros deben revisarse pronto, pero activarse por fases:

- `AI_THRESHOLD`
- `BUY_SOFT_SCORE_MIN`
- `MIN_AGE_MIN`
- `MIN_HOLDERS`
- `MIN_LIQUIDITY_USD`
- `MIN_VOL_USD_24H`
- `MAX_MARKET_CAP_USD`
- `REQUIRE_JUPITER_FOR_BUY`
- `MAX_ACTIVE_POSITIONS`
- `BUY_RATE_LIMIT_N`
- `BUY_RATE_LIMIT_WINDOW_S`
- `TP_PARTIAL_FRACTION`
- `POST_PARTIAL_STOP_PCT`
- `NO_PUMP_WINDOW_MIN`
- `NO_PUMP_MIN_PNL_PCT`
- `TIME_STOP_MIN`
- `TIME_STOP_MIN_PEAK_PCT`
- `TIME_STOP_MAX_PNL_PCT`

---

## Definicion de exito final

El proyecto de mejora se considerara exitoso si al final:

1. El bot mide correctamente sus resultados.
2. El dataset ya sirve para aprender algo util.
3. Compra menos ruido.
4. Mantiene o mejora la captura de winners fuertes.
5. Reduce perdidas absurdas por entrada mala o colapso de liquidez.
6. El despliegue se hace sin romper la operativa actual.

---

## Siguiente paso recomendado

Empezar por **PR 1 + PR 2**:

- baseline y reporting
- correccion completa de PnL/labels de parciales

Ese es el mejor primer paso porque:

- no depende de tener modelo
- no cambia aun el edge
- arregla la base de verdad del sistema
- permite medir bien cualquier optimizacion posterior

