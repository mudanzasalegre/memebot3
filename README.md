![MemeBot 3 banner](assets/memebot3img.jpg)

# MemeBot 3

MemeBot 3 es un bot y workstation de investigacion para meme-coins de Solana.
Combina discovery, enriquecimiento de mercado, filtros por regimen, paper/live
execution, gestion de posiciones, analitica local, modelos ML en sombra y un
loop AutoResearch para proponer y validar cambios de estrategia sin activar
live de forma automatica.

El proyecto esta pensado para operar primero en paper, medir edge con reports y
replay, y solo despues considerar canaries live manuales y pequenos.

## Estado Actual

### Valoracion SOL/USD y evidencia historica

La conversion compartida exige un precio SOL/USD numerico positivo, el
`last_updated_at` de CoinGecko y el recibo HTTP original. La edad del recibo se
limita a `COINGECKO_SOL_TTL` (1..60 s) y la del dato de mercado a
`COINGECKO_SOL_MAX_MARKET_AGE_S` (1..300 s; defecto 120 s). Son limites de
aceptacion del cliente, no garantias del proveedor ni de ejecucion de swaps.
Un fallo de refresco deja la conversion desconocida: no se reutiliza
indefinidamente el ultimo precio bueno. Las consultas concurrentes comparten
el recibo original y los errores tienen un cooldown de 10 s.

`COINGECKO_DEMO_API_KEY` es opcional para configurar el acceso al endpoint
publico; la documentacion Demo lo requiere. Sin acceso aceptado no se inventa
precio y no se garantiza que se puedan valorar operaciones. La clave se envia
solo a `api.coingecko.com`, sin redirecciones. No se ha probado el acceso real
del operador en esta revision.

`SOL_USD_OVERRIDE` queda identificado como supuesto de escenario, no como
evidencia de mercado. `get_sol_usd()` y las conversiones financieras normales
lo rechazan; solo una consulta diagnostica explicita con
`allow_assumed=True` puede consumirlo. Los parametros opcionales invalidos
utilizan valores acotados por defecto y no rompen el primer import.

PAPER y las dos investigaciones prospectivas revisan SOL/USD de nuevo tras
esperar una cotizacion; si no existe, no registran un fill ni una ganancia.
Una compra historica sin su importe USD original no se reconstruye con el
cambio actual. El canary live no convierte con un SOL ficticio de 1 USD ni
reinicia una racha de perdidas ante un resultado desconocido: bloquea nuevas
admisiones. La via green LIVE reconstruye ahora un registro duradero desde
los buy/sell journals originales y las identidades SQL, antes del arranque y
de cada admision. Reserva el presupuesto antes de ejecutar, sin sobrepasar
el tamano LIVE aprobado ni el principal restante del limite diario.
Duplicados no incrementan contadores; fuentes perdidas, cambiadas, cierres
sin cobertura y finalidades pendientes no se convierten en riesgo cero.

El presupuesto usa lamports enteros y caja nativa conservadora de los recibos
independientes originales: no usa PnL USD convertido con el cambio actual.
Incluye las comisiones de red de la wallet y no acredita mas que el principal
neto comprobado cuando hay rent/depositos o saldos wrapped. Es una medida de
riesgo, no un certificado de beneficio; las comisiones futuras pueden hacer
que una perdida efectiva supere el principal reservado. El registro requiere
el lock de instancia del bot, conserva las fuentes y no activa LIVE. PAPER
no crea ni escribe este registro. Los otros limites de estrategia/capital y
la aceptacion financiera completa siguen siendo fronteras independientes.
Vease `docs/audits/green_canary_risk_20261008.json` y sus pruebas aisladas.

La ejecucion PAPER principal conserva ahora `SolUsdObservation` y el reloj
original de cada compra y de cada venta, incluidos los parciales. Estos
recibos pasan por buy/sell recovery, close outbox, archivo, fuente neta del
modelo y los dos bancos prospectivos. Cada conversion se valida en su reloj
original; un scalar, supuesto o recibo perdido no se convierte en beneficio.
Los bancos solo reutilizan la cotizacion tras persistir el fill principal,
sin solicitudes adicionales ni nuevos intents. Un fallo de un banco no
bloquea el otro ni deshace la venta. El replay PAPER comprueba el modo SQL
sin cambiarlo. Los historicos sin estos recibos siguen sin certificarse y
no se reescriben. Vease `docs/audits/paper_execution_fx_20261009.json`.

La investigacion de runners reconstruye tambien la caja del primer parcial
desde su recibo original: cantidad exacta, cotizacion inversa, slippage y las
comisiones de compra/venta valoradas con el cambio original de cada fill.
Conserva esa prueba publica en los casos y la comprueba de nuevo al comparar
salidas o consumir una politica seleccionada. Un hash recalculado no valida
totales financieros alterados. Las fuentes antiguas sin prueba completa se
conservan, pero no certifican ni financian esta investigacion. Vease
`docs/audits/paper_first_partial_cash_20261009.json`.

El cierre PAPER cotizado reconstruye ahora cada venta desde su cotizacion
original, cantidad, reloj y FX. Suma los cobros SOL/USD y las comisiones de
cada fill, y contrasta el beneficio neto guardado antes de archivarlo, usarlo
como evidencia prospectiva o convertirlo en etiqueta de aprendizaje. Los
parciales se conservan separados de la venta final; ni un hash recalculado
ni cifras coherentes entre si sustituyen esa prueba de caja. No se consultan
precios actuales ni se reparan historicos. Vease
`docs/audits/paper_closed_cash_20261009.json`.

Cada compra PAPER conserva ahora una copia separada y versionada de sus
supuestos originales de slippage/comision y de su reloj. La respuesta y el
journal retienen esa misma base, tambien al recuperar una respuesta perdida.
Ventas, archivos, investigacion de parciales y etiquetas financieras rechazan
costes declarados que cambian despues de la compra. Las fuentes o modelos
financieros antiguos sin esa procedencia no se recertifican ni se reescriben;
siguen siendo diagnosticos. Esto evita convertir una perdida en una etiqueta
de ganancia eliminando comisiones de forma coherente, pero no autentica
recibos contra un proveedor ni demuestra beneficios LIVE. Vease
`docs/audits/original_paper_cost_basis_20261009.json`.

Estas correcciones no certifican rentabilidad: la alineacion de las marcas
de runners y de la investigacion de entradas con su propia caja cotizada ya
esta integrada. La procedencia de la estrategia completa, el cambio original
de los fills LIVE y la aceptacion financiera prospectiva siguen
pendientes. Las compras de 0.1 SOL PAPER y los runners sin techo de beneficio
no se han cambiado. Vease `docs/audits/sol_usd_quality_20261008.ipynb` para las
comprobaciones reproducibles con datos sinteticos, no trades del operador.

El checkout actual incluye:

- Bot async principal en `run_bot.py`.
- Backend FastAPI con API operacional protegida por login local.
- UI React/Vite como cockpit de operador.
- Feature store parquet, SQLite runtime DB, JSONL de eventos y reports locales.
- ML tabular con validaciones, entrenamiento, threshold tuning y modo sombra por defecto.
- AutoResearch end-to-end: safety, schema, objectives, report bundle, API budget,
  sandbox, replay, evaluator, scoreboard, search spaces, batch, checkpoint,
  bandit, paper-forward, rollback, scheduler, LLM adapter deshabilitado,
  API/UI read-only y smoke/gates finales.

## Aviso De Riesgo

Este proyecto puede ejecutar swaps reales si `DRY_RUN=0` y los perfiles live lo
permiten. Las meme-coins pueden perder liquidez de forma inmediata. Usa una
hot wallet dedicada, empieza con paper, valida la UI y no deposites fondos que
no puedas perder.

AutoResearch no activa live. Genera y valida artefactos de research/paper; la
promocion live queda fuera del loop automatico.

## Arquitectura

```text
Fetchers
  DexScreener, Pump.fun/PumpPortal, GeckoTerminal, Jupiter,
  Helius/RPC, RugCheck, Birdeye, GMGN
      |
      v
Discovery queue
  filtros iniciales, backoff, retry, route/liquidity checks
      |
      v
Feature builder
  SQLite + parquet + runtime_events/candidate_outcomes/decision_ledger
      |
      v
Strategy runtime
  regimenes, lanes, gates, health, bucket blocks, ML shadow, policy state
      |
      v
Execution
  paper trading o swaps reales via rutas configuradas
      |
      v
Position monitor
  partial TP, runner floors, adverse tick, no-pump, liquidity crush, stops
      |
      v
Analytics, API, UI, AutoResearch
  reports locales, replay, objective score, scoreboard, API budget y gates
```

## Componentes Principales

| Ruta | Funcion |
| --- | --- |
| `run_bot.py` | Loop principal: discovery, scoring, gating, buy/sell, monitor, retrain, telemetry. |
| `config/config.py` | Carga `.env` y expone configuracion runtime. |
| `analytics/` | Filtros, runtime strategy, reports, scorecards, exits, sniper, runner capture. |
| `runtime/` | Estado runtime, command bus, process manager, policy modes, hot queue, paper-forward helpers. |
| `features/` | Construccion y escritura del feature store parquet. |
| `ml/` | Entrenamiento, modelos, registry, policy, risk/EV/runner/continuation models. |
| `trader/` | Paper trading y ejecucion real de buyer/seller. |
| `api/` | Backend FastAPI para UI y endpoints operacionales. |
| `ui/` | UI React/Vite de operador. |
| `research_loop/` | AutoResearch paper/replay-only. |
| `tools/` | Herramientas de reports, replay, AutoResearch y auditoria. |
| `scripts/` | Arranque local, smoke tests, quality gates, backup/restore. |
| `strategy_proposals/` | Schemas y candidate policies. |
| `data/` | DB, metrics, features, research runs. No deberia subirse a GitHub. |
| `logs/` | Logs runtime locales. No deberia subirse a GitHub. |

## Requisitos

| Requisito | Notas |
| --- | --- |
| Python | 3.10+, probado aqui con Python 3.12. |
| Node.js + npm | Necesario para la UI. |
| PowerShell | Los scripts de arranque son Windows-first. |
| Solana RPC | Helius u otro RPC fiable si se usa live o enriquecimiento avanzado. |
| Credenciales externas | Opcionales segun provider y modo. Paper puede arrancar con menos. |

Instalar dependencias Python:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Instalar UI:

```powershell
cd ui
npm install
cd ..
```

## Configuracion Y Secretos

Copia `.env.example` a `.env` y rellena solo lo que uses:

```powershell
Copy-Item .env.example .env
notepad .env
```

Variables relevantes:

| Variable | Uso |
| --- | --- |
| `DRY_RUN` | `1` paper, `0` live. |
| `SOL_PUBLIC_KEY` | Wallet publica. |
| `SOL_PRIVATE_KEY` | Necesaria solo para swaps reales. Usa hot wallet dedicada. |
| `SOL_RPC_URL`, `RPC_URL`, `HELIUS_RPC_URL` | RPC Solana. |
| `HELIUS_API_KEY`, `BIRDEYE_API_KEY`, `RUGCHECK_API_KEY` | Enriquecimiento de datos. |
| `PUMPPORTAL_API_KEY` | PumpPortal websocket discovery si esta habilitado. |
| `JUP_API_KEY` | Opcional: gateway actual con o sin clave; una clave inválida no se descarta para reintentar. |
| `JUP_API_RPS` | Vacío: 0.5 sin clave / 1 con clave. Con clave, ajustar solo al cupo real del plan contratado. |
| `UI_AUTH_MODE`, `UI_LOCAL_USERS`, `UI_SESSION_SECRET` | Login local de la UI/API. |

No subas `.env`, claves privadas, backups con secretos, `data/` ni `logs/`.

## Modos Runtime

| Modo | Como | Efecto |
| --- | --- | --- |
| Paper | `DRY_RUN=1` o `.\scripts\start_bot.ps1` | No envia swaps reales; usa `trader.papertrading`. |
| Real | `DRY_RUN=0` o `.\scripts\start_bot.ps1 -RealMode` | Puede enviar swaps reales. |
| Shadow | ML/lane en sombra | Registra decision/outcome sin afectar gating productivo. |
| Live canary | Flags live + caps + aprobacion operacional | Rollout live controlado y manual. |
| AutoResearch | `research_loop/` + `tools/run_autoresearch_loop.py` | Research local/paper; live auto prohibido. |

Base recomendada para desarrollo:

```env
DRY_RUN=1
STRATEGY_OPTIMIZATION_LOCK=true
ML_GATE_MODE=shadow
AUTO_PROMOTE_LIVE=false
MODEL_AUTO_PROMOTE=false
```

## Arranque Rapido

API + UI:

```powershell
.\scripts\start_stack.ps1
```

API + UI + bot en paper:

```powershell
.\scripts\start_stack.ps1 -IncludeBot
```

Ese comando arranca solo el bot en paper. AutoResearch es explicito y se
incluye con:

```powershell
.\scripts\start_stack.ps1 -IncludeBot -IncludeAutoResearch
```

AutoResearch arranca en modo daemon paper/replay seguro. Por defecto usa
`AutoResearchMaxCandidates=3`, `MaxParallel=1` e intervalo de 360 minutos. El
stack limpia locks muertos del bot, regenera core reports antes de levantar
AutoResearch, refresca reports antes de los ciclos de investigacion, mantiene
live promotion apagado y bloquea LLM live-touch.

Para una sola iteracion:

```powershell
.\scripts\start_stack.ps1 -IncludeBot -IncludeAutoResearch -AutoResearchOnce
```

Opciones utiles:

```powershell
.\scripts\start_stack.ps1 -IncludeBot -IncludeAutoResearch -AutoResearchSpace moonshot_micro -AutoResearchMaxCandidates 10
.\scripts\start_stack.ps1 -IncludeBot -IncludeAutoResearch -AutoResearchOnce -AutoResearchNoPaperPromote
.\scripts\start_stack.ps1 -IncludeBot -IncludeAutoResearch -AutoResearchSkipRegenerateReports
```

API + UI + bot en real mode:

```powershell
.\scripts\start_stack.ps1 -IncludeBot -BotRealMode
```

Servicios:

| Servicio | URL |
| --- | --- |
| UI | `http://127.0.0.1:5173` |
| API docs | `http://127.0.0.1:8000/docs` |
| API health | `http://127.0.0.1:8000/api/v1/health` |

Componentes por separado:

```powershell
.\scripts\start_api.ps1
.\scripts\start_ui.ps1
.\scripts\start_bot.ps1
.\scripts\start_autoresearch.ps1
```

Bot directo:

```powershell
.\.venv\Scripts\python.exe run_bot.py --dry-run --log
```

## UI Operacional

La UI es un cockpit, no una landing page. Rutas principales:

| Pagina | Objetivo |
| --- | --- |
| `Overview` | Estado diario: runtime, queue, wallet, ML, source truth, posiciones. |
| `Runtime` | Heartbeat, flags de pausa, strategy health, buy limiter. |
| `Sniper` | Green sniper, hot queue, missed pumps, postura live-canary. |
| `Discovery` | Funnel de rejects/waits/shadows/buys y motivos. |
| `Queue` | Backlog, retries, oldest candidate y presion de cola. |
| `Positions` | Posiciones abiertas y riesgo. |
| `Trades` | Ledger cerrado y filtros. |
| `Trade Replay` | Reconstruccion de una operacion desde T0 a cierre. |
| `Analytics` | Edge por exits, lanes, regimenes y cobertura. |
| `ML Center` | Modelo, dataset quality, thresholds, readiness y blockers. |
| `Policy Center` | Safety gates, replay, decision ledger, funnel attribution y proposals. |
| `AutoResearch` | Scoreboard, current best, API budget, moonshot progress, paper-forward. |
| `Config Center` | Config efectiva y policies derivadas. |
| `Logs and Events` | Logs, runtime events y research events. |
| `Control Center` | Command bus, pause/resume, refresh reports, retrain, process control. |

## API

La API usa envelopes normalizados:

```json
{
  "data": {},
  "meta": {
    "generated_at": "2026-06-04T00:00:00+00:00",
    "degraded": false,
    "empty": false,
    "stale": false,
    "source_status": []
  }
}
```

`/api/v1/health` y auth son publicos. El resto requiere sesion local.

Endpoints principales:

| Endpoint | Uso |
| --- | --- |
| `GET /api/v1/health` | Health API. |
| `GET /api/v1/auth/session` / `POST /api/v1/auth/login` | Sesion local. |
| `GET /api/v1/sources/status` | Estado de SQLite, JSONL, parquet y metrics. |
| `GET /api/v1/overview` | Cockpit principal. |
| `GET /api/v1/runtime/state` | Runtime snapshot. |
| `GET /api/v1/runtime/strategy-health` | Salud de regimen/lane/buckets. |
| `GET /api/v1/discovery/feed` / `summary` | Funnel discovery. |
| `GET /api/v1/queue/summary` / `items` | Cola runtime. |
| `GET /api/v1/positions/open` | Posiciones abiertas. |
| `GET /api/v1/trades/closed` | Trades cerrados. |
| `GET /api/v1/trades/{trade_id}` / `replay` | Trade factsheet y replay. |
| `GET /api/v1/analytics/edge` / `baseline` | Reports de edge. |
| `GET /api/v1/config/effective` / `policies` | Config y policies efectivas. |
| `GET /api/v1/ml/status` / `research` | Estado ML y research scorecard. |
| `GET /api/v1/policy/*` | Learned-policy safety/replay/ledger/funnel/proposals. |
| `GET /api/v1/sniper/*` | Sniper status, missed pumps y hot queue. |
| `GET /api/v1/socials/*` | Social enrichment. |
| `GET /api/v1/research/*` | AutoResearch read-only. |
| `GET /api/research/*` | Alias read-only para AutoResearch. |
| `GET /api/v1/logs/tail` | Log tail permitido. |
| `GET /api/v1/events/runtime` / `research` | JSONL event rails. |
| `GET/POST /api/v1/control/*` | Command bus y process manager. |
| `GET/POST/PATCH/DELETE /api/v1/saved-views` | Views locales UI. |

AutoResearch endpoints:

```text
/api/v1/research/scoreboard
/api/v1/research/runs
/api/v1/research/current-best
/api/v1/research/api-budget
/api/v1/research/moonshot-progress
/api/v1/research/paper-forward
```

## Estrategia, Lanes Y Exits

El bot separa PnL productivo de investigacion:

| Lane / familia | Funcion |
| --- | --- |
| `pump_early_pumpswap_profit` | Lane productiva principal con gates estrictos. |
| `pump_early_pumpswap_prime` / meteor-prime | Tags internos de mayor edge dentro de pumpswap. |
| Green sniper / birth probe | Captura temprana y auditoria de newborn pumps. |
| Rank canary | Paper/research basado en ranking. |
| Shadow follow-up micro | Follow-up paper sobre señales shadow. |
| Moonshot micro | Micro-lottery paper para captura de tail events. |
| Late momentum micro | Paper/research sobre momentum tardio. |
| Dex mature / revival shadow | Investigacion y scorecards sin contaminar PnL productivo. |

El motor de exits incluye partial TP, post-partial protection, runner floors,
adverse tick, no-pump exit, liquidity crush, stop loss, time stop y total PnL
protection. Los reports de runner capture y missed pumps alimentan AutoResearch.

El objetivo de runners no se limita al +100% o +200%: incluye +500%, +1.000%,
+5.000% y superiores. Los umbrales de aprendizaje no son techos de venta.
Las posiciones PAPER de 0,1 SOL conservan la politica congelada de parciales y
proteccion de precio, sin prometer capturar el maximo ni eliminar el riesgo.

La investigacion prospectiva de entradas valora su propia cantidad restante,
no el precio de pantalla. Conserva el recibo original SOL/USD de la entrada y
los fills nuevos, y separa la cotizacion de valoracion de la posterior venta
virtual. Los casos sin prueba financiera completa no certifican el autoajuste.
El presupuesto compartido sigue acotado; una falta de cobertura se rechaza,
no se convierte en una ganancia o perdida ficticia. El hook de la salida PAPER
principal aun entrega solo un cambio escalar y no puede certificar estos fills
nuevos; la sonda de investigacion con recibo original funciona por separado.
Evidencia de software: `docs/audits/entry_gate_cash_forward_20261008.json`.

Las valoraciones PAPER principales ya comparten su cotizacion exacta y el
recibo SOL/USD original con los bancos de entrada y runner compatibles.
Cada brazo conserva su propia base financiera y solo acepta el mismo token,
cantidad raw y cronologia. La reutilizacion no consume una sonda adicional
ni ejecuta una intencion de venta recien creada. El polling pareado deja de
consultar por defecto un precio spot que no certifica ese efectivo.
La cuota no garantiza cobertura exhaustiva: una simulacion con la reserva
original obtiene huecos de 360 segundos con 3 grupos de entrada y 1 runner,
frente al limite financiero de 300 segundos. Son escenarios sinteticos,
no trades ni rentabilidad observada. La aceptacion sigue rechazando huecos.
Evidencia: `docs/audits/research_cash_reuse_20261008.json` y el notebook
`docs/audits/research_quote_capacity_20261008.ipynb`.

Una cotizacion no disponible no equivale a ausencia de ruta. Las sondas
revalidan el payload, el importe y el recibo original (maximo 10 segundos);
red, cuota, auth, payload invalido o caducidad quedan desconocidos. Si la ruta
es obligatoria, se aplaza la decision completa, sin permiso de compra ni
penalizacion negativa de aprendizaje. La cola conserva limites de edad y
capacidad. Solo un rechazo previo al envio confirmado en el diario original
puede volver a esa cola; un envio incierto sigue en recuperacion, sin reintento.
Los limites de impacto, capital y configuracion no se relajan. Evidencia:
`docs/audits/route_observation_admission_20261008.json` (software, no rentabilidad).

Con `JUP_MANAGED_ENABLED=true`, el scanner, las entradas/salidas PAPER y la
investigacion prospectiva consultan Swap V2 sin `taker`: Metis, JupiterZ,
DFlow y OKX. No se solicita wallet ni transaccion para firmar. Una cotizacion
RFQ/externa puede tener cero pasos publicados; solo se admite con su contrato
completo validado, sin inventar pasos Metis. Los recibos publicos conservan
payload permitido, importe, router y hora originales a traves del archivo y
las etiquetas de aprendizaje. No son prueba de ejecucion ni de liquidez actual.
La orden real sigue su validacion y reconciliacion independientes. Desactivar
explicitamente el modo managed conserva Metis V1, sin fallback silencioso entre
protocolos. Evidencia: `docs/audits/jupiter_all_router_observations_20261008.json`.

Price V3 es evidencia de precio, no de ruta: `has_route` y `routes_count`
permanecen desconocidos. Solo una omision en un mapa V3 valido entra en la
cache negativa; HTTP, JSON, campos duplicados, unidades invalidas o payloads
mal formados quedan como ERR y se pueden consultar de nuevo. Se limita el
cuerpo a 1 MiB y se conserva el recibo HTTP original tambien en cache, junto
con `blockId` y `decimals` cuando existen. El recibo no demuestra la recencia
del ultimo swap; los atajos fijos de estables son supuestos, no observaciones.
La policy de precio fiable y las cotizaciones independientes no se relajan.
Evidencia reproducible: `docs/audits/jupiter_price_quality_20261008.ipynb`.

## Machine Learning

ML es opcional y seguro por defecto:

```env
ML_GATE_MODE=shadow
AI_SIZING_ENABLED=false
MODEL_AUTO_PROMOTE=false
ML_AUTO_PROMOTE_LANES=false
```

Fuentes:

| Fuente | Uso |
| --- | --- |
| `data/features/features_YYYYMM.parquet` | Snapshots de features. |
| SQLite `positions` | Resultados cerrados. |
| `data/metrics/candidate_outcomes.jsonl` | Research/shadow outcomes. |
| `data/metrics/decision_ledger.jsonl` | Ledger canonico de decisiones. |
| `ml/model.pkl`, `ml/model.meta.json` | Modelo activo. |
| `data/metrics/train_status.json` | Estado ultimo entrenamiento. |
| `data/metrics/dataset_quality.json` | Calidad dataset. |

Comandos:

```powershell
.\.venv\Scripts\python.exe -m ml.retrain
.\.venv\Scripts\python.exe scripts\ml_report.py
.\.venv\Scripts\python.exe tools\ml_status.py
```

## AutoResearch

AutoResearch adapta el patron de experimentos tipo `autoresearch` a trading
sin permitir que el agente edite runtime live ni ejecute swaps reales.

Flujo:

```text
reports locales
-> report_bundle
-> api_budget
-> candidate_policy
-> safety/schema
-> sandbox candidate.env
-> replay local
-> objective_score
-> evaluator
-> scoreboard
-> paper-forward opcional
-> accepted/rejected
```

Superficie permitida:

- Candidate policies.
- Perfiles sandbox/paper sin secretos.
- Reports bajo `data/research_runs/`.
- Search spaces de thresholds, sizing paper y exits.
- Batch, checkpoint, bandit y scheduler.

Superficie prohibida:

- Activar live.
- Cambiar `.env` real.
- Tocar buyer/seller/wallet/signer.
- Tocar claves, RPC secrets o API keys.
- Aumentar rate limits o frecuencia de discovery.
- Desactivar risk guards.
- Usar LLM como trader.

Modulos:

| Archivo | Funcion |
| --- | --- |
| `research_loop/safety.py` / `safety.yaml` | Contrato de seguridad. |
| `research_loop/experiment_schema.py` | Schema candidate policy. |
| `research_loop/objectives.py` / `objectives.yaml` | Objective score y hard gates. |
| `research_loop/report_bundle.py` | Bundle local de reports. |
| `research_loop/api_budget.py` | Budget local, 429s, degraded providers. |
| `research_loop/sandbox.py` | `candidate.env` aislado sin secretos. |
| `research_loop/replay_runner.py` | Replay local y snapshot de reports. |
| `research_loop/evaluator.py` | `accepted_replay`, `needs_paper`, `rejected`, `failed`, `inconclusive`. |
| `research_loop/scoreboard.py` | `scoreboard.json` y `scoreboard.md`. |
| `research_loop/search_space.py` + `spaces/` | Registry y optimizadores especializados. |
| `research_loop/candidate_generator.py` | Grid/random/local/bandit candidates. |
| `research_loop/batch_runner.py` / `checkpoint.py` | Batches y resume/duplicates. |
| `research_loop/bandit.py` | Seleccion de espacios por reward. |
| `research_loop/paper_forward.py` | Controller paper-forward. |
| `research_loop/policy_promoter.py` | Perfil paper seguro. |
| `research_loop/rollback.py` | Rollback de paper candidate degradado. |
| `research_loop/scheduler.py` | Loop continuo, idle trigger y demotion. |
| `research_loop/llm_adapter.py` | Adapter opcional, disabled por defecto. |
| `research_loop/smoke.py` | Smoke end-to-end. |

Comandos AutoResearch:

```powershell
.\.venv\Scripts\python.exe tools\api_budget_report.py
.\.venv\Scripts\python.exe tools\generate_research_candidates.py --space moonshot_micro --n 25 --seed 42
.\.venv\Scripts\python.exe tools\run_research_replay.py strategy_proposals\candidates\PROPOSAL_ID.json
.\.venv\Scripts\python.exe tools\run_research_batch.py --space moonshot_micro --n 50 --seed 42
.\.venv\Scripts\python.exe tools\research_scoreboard.py
.\.venv\Scripts\python.exe tools\start_research_paper.py strategy_proposals\candidates\PROPOSAL_ID.json
.\.venv\Scripts\python.exe tools\finalize_research_paper.py PAPER_RUN_ID
.\.venv\Scripts\python.exe tools\run_autoresearch_loop.py --once --space moonshot_micro --max-candidates 3
.\.venv\Scripts\python.exe tools\autoresearch_smoke.py
```

Search spaces actuales:

```text
rank_canary
shadow_followup / shadow_followup_micro
moonshot_micro
runner_exit / runner_ladder
entry_quality
late_momentum
lane_sizing
sniper_momentum
paper_exploration
```

Docs:

- `docs/AUTORESEARCH_MEMEBOT.md`
- `docs/AUTORESEARCH_RUNBOOK.md`
- `research_loop/program.md`
- `research_loop/runbook.md`

## Datos Y Artefactos

| Ruta | Descripcion |
| --- | --- |
| `data/memebotdatabase.db` | SQLite runtime: tokens, positions, state, commands, views. |
| `data/features/features_YYYYMM.parquet` | Feature store. |
| `data/metrics/*.json` | Reports generados. |
| `data/metrics/*.jsonl` | Runtime/research/social/decision event rails. |
| `data/research_runs/runs/` | Sandboxes y replay runs AutoResearch. |
| `data/research_runs/batches/` | Batches AutoResearch. |
| `data/research_runs/paper_forward/` | Estados paper-forward. |
| `data/research_runs/scoreboard.json` | Scoreboard AutoResearch. |
| `data/research_runs/api_budget.json` | Budget API AutoResearch. |
| `config/profiles/*.env` | Perfiles paper/live/research. No contienen secrets si son generados por AutoResearch. |
| `logs/*.txt` | Logs runtime. |

## Reports

Regenerar reports core:

```powershell
.\.venv\Scripts\python.exe tools\regenerate_core_reports.py
```

Reports habituales:

```powershell
.\.venv\Scripts\python.exe scripts\baseline_report.py
.\.venv\Scripts\python.exe scripts\edge_report.py
.\.venv\Scripts\python.exe scripts\ml_report.py
.\.venv\Scripts\python.exe scripts\pnl_rollout_report.py
.\.venv\Scripts\python.exe tools\trade_diagnostics.py
.\.venv\Scripts\python.exe tools\missed_pumps_report.py
.\.venv\Scripts\python.exe tools\runner_capture_ladder_report.py
```

## Quality Gates Y Tests

Suite completa:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Gate local completo:

```powershell
.\.venv\Scripts\python.exe scripts\quality_gate.py
```

Gate backend/API sin build UI:

```powershell
.\.venv\Scripts\python.exe scripts\quality_gate.py --skip-ui-build
```

Strategy gate:

```powershell
.\.venv\Scripts\python.exe scripts\strategy_quality_gate.py --warn-only
```

AutoResearch final smoke/gate:

```powershell
.\.venv\Scripts\python.exe tools\autoresearch_smoke.py
.\.venv\Scripts\python.exe scripts\strategy_quality_gate.py --warn-only
```

UI build:

```powershell
cd ui
npm run build
cd ..
```

Ultima validacion conocida de este README:

```text
pytest: 472 passed
strategy_quality_gate: ok
autoresearch_smoke: ok
ui build: ok
```

## Backup Y Restore

Crear backup runtime:

```powershell
.\scripts\backup_runtime.ps1
```

Restaurar:

```powershell
.\scripts\restore_runtime.ps1 .\backups\memebot3-backup-YYYYMMDD-HHMMSS.zip --force
```

Incluye `.env` solo si entiendes que el backup contendra secretos:

```powershell
.\scripts\backup_runtime.ps1 --with-env --with-logs
.\scripts\restore_runtime.ps1 .\backups\memebot3-backup-YYYYMMDD-HHMMSS.zip --force --with-env
```

## Operacion Diaria

1. Abrir `http://127.0.0.1:5173`.
2. Revisar `Overview`: sources, runtime, queue, positions, ML.
3. Revisar `Runtime`: heartbeat, pause flags, strategy health y cooldowns.
4. Revisar `Discovery`: principales motivos de reject/wait/shadow.
5. Revisar `Sniper`: hot queue, missed pumps y postura de canary.
6. Revisar `Analytics`: closed trades, median PnL, exits y runner capture.
7. Revisar `AutoResearch`: scoreboard, current best, API budget y paper-forward.
8. Usar `Trade Replay` para perdidas grandes o runners importantes.

Si no hay buys:

1. Confirmar `buys_paused=false` y `discovery_paused=false`.
2. Revisar `Runtime -> Strategy Health`.
3. Revisar `Discovery -> Summary`.
4. Verificar que ML esta en shadow o en el modo esperado.
5. Mirar `logs/*`, `runtime_events.jsonl`, `candidate_outcomes.jsonl` y API budget.

## Checklist Antes De Live

1. `DRY_RUN=0` solo si es intencional.
2. Hot wallet dedicada y con fondos minimos.
3. `AUTO_PROMOTE_LIVE=false` y `MODEL_AUTO_PROMOTE=false`.
4. AutoResearch live promotion deshabilitado.
5. UI sin sources stale.
6. Strategy health sin cooldown critico.
7. API budget sin degradacion nueva.
8. Tamano pequeno y caps live revisados.
9. ML no enforce salvo validacion explicita.
10. Monitorizar primeras operaciones desde UI y logs.

## GitHub Hygiene

Subir:

```text
README.md
.env.example
source code
tests
docs publicos
```

No subir:

```text
.env
data/
logs/
backups con .env
wallet keys
API keys
modelos privados si no quieres publicarlos
```

`.gitignore` recomendado:

```gitignore
.env
.venv/
__pycache__/
.pytest_cache/
data/
logs/
backups/
ml/*.pkl
ml/*.meta.json
ui/node_modules/
ui/dist/
*.bkup.*
```

## Documentacion

| Documento | Uso |
| --- | --- |
| `docs/API_UI_SPEC.md` | Contrato API/UI. |
| `docs/UI_OPERATIONS.md` | Runbook UI/API/bot. |
| `docs/UI_SITEMAP.md` | Mapa UI. |
| `docs/UI_STATE_CONTRACT.md` | Source/state contract. |
| `docs/AUTORESEARCH_MEMEBOT.md` | Vision AutoResearch. |
| `docs/AUTORESEARCH_RUNBOOK.md` | Smoke y gate AutoResearch. |
| `docs/SNIPER_RUNBOOK.md` | Sniper runbook. |
| `docs/ML_REPORT.md` | Report ML generado. |
| `docs/EDGE_REPORT.md` | Edge report generado. |
| `docs/ROLLOUT_REPORT.md` | Rollout report generado. |

## Licencia

MIT. Ver detalles del repositorio original de `mudanzasalegre`.
