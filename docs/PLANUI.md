# PLANUI

## Objetivo

Construir una UI operativa real para MemeBot 3, con tres capas separadas:

1. bot runtime como proceso autonomo
2. backend API como capa de lectura y control
3. frontend web como consola de operaciones

La meta no es un dashboard bonito. La meta es poder:

- vigilar el runtime sin abrir SQLite ni logs a mano
- entender discovery, cola, filtros, buys, exits, PnL, sizing, ML y config
- reconstruir un trade desde T0 hasta el cierre
- ejecutar acciones operativas seguras desde una UI
- mantener el bot funcionando aunque la UI o la API fallen

---

## Alcance de este documento

Este documento sustituye un plan demasiado aspiracional por un plan aterrizado al repo real, verificado el **31 de marzo de 2026**.

La conclusion principal es simple:

- la UI read-only se puede empezar ya con los datos persistidos que existen
- la UI live de verdad necesita que el bot publique snapshots persistentes de su estado en memoria
- la gestion desde interfaz grafica requiere un command bus explicito y auditable

---

## Diagnostico real del proyecto

### Runtime real hoy

El bot actual vive sobre todo en `run_bot.py` y orquesta:

1. discovery periodico por DexScreener
2. stream de nuevos tokens por Pump.fun
3. validacion de cola en `utils/lista_pares.py`
4. scoring, gating ML, sizing y compra
5. monitor de posiciones abiertas y ejecucion de exits
6. research lane y shadow decisions
7. retraining loop
8. labeler periodico

### Datos persistidos que ya existen

Fuentes que una API puede leer hoy sin tocar el loop del bot:

- SQLite:
  - `tokens`
  - `positions`
  - `revived_tokens`
- JSONL:
  - `data/metrics/runtime_events.jsonl`
  - `data/metrics/candidate_outcomes.jsonl`
- JSON:
  - `data/metrics/research_scorecard.json`
  - `data/metrics/research_thresholds.json`
  - opcionales de ML: `dataset_quality.json`, `train_status.json`, `recommended_threshold.json`
- Parquet:
  - `data/features/features_YYYYMM.parquet`
- logs:
  - `logs/*.txt`
- paper mode:
  - `data/paper_portfolio.json` si existe

### Estado real del workspace hoy

Inventario verificado localmente:

- `data/memebotdatabase.db` existe
- las tablas `tokens`, `positions` y `revived_tokens` existen
- en este instante la DB actual tiene `0` filas en esas tres tablas
- `data/metrics/runtime_events.jsonl` si tiene eventos recientes
- `data/metrics/candidate_outcomes.jsonl` si tiene decisiones recientes
- `data/features/features_202603.parquet` existe
- `data/paper_portfolio.json` no existe ahora mismo

### Implicacion importante

Los ficheros en `docs/BASELINE.md`, `docs/EDGE_REPORT.md` y `docs/ML_REPORT.md` son snapshots generados y pueden quedar desfasados respecto al estado real de `data/` y `db/`.

La UI no debe leer `docs/*.md` como fuente de verdad.

La UI debe leer:

- DB
- JSONL
- JSON de metrics
- parquet
- logs

### Piezas reutilizables ya presentes

El repo ya trae mucha logica util para una API:

- `analytics/reporting.py`
  - baseline
  - edge summary
  - joins entre DB, parquet y runtime events
- `analytics/filters.py`
  - thresholds efectivos
  - snapshot quality gate
  - descripcion de policy
- `analytics/exit_policy.py`
  - policy efectiva de exits
- `analytics/sizing.py`
  - policy de sizing y caps por regimen
- `analytics/ai_predict.py`
  - estado del modelo en runtime de ficheros
- `analytics/research_runtime.py`
  - scorecard y thresholds de research
- `fetcher/jupiter_price.py`
  - status de precio/ruta
- `db/database.py`
  - init y migracion defensiva de SQLite

### Lo que no existe todavia

- no hay backend HTTP
- no hay frontend
- no hay contrato de API
- no hay snapshots persistentes del estado live del runtime
- no hay heartbeat persistente del bot
- no hay snapshot persistente de cola
- no hay snapshot persistente de wallet
- no hay command bus
- no hay tabla de comandos
- no hay auth, sesiones o RBAC
- no hay saved views
- no hay alert manager real

---

## Restricciones tecnicas no negociables

### 1. La API no debe importar `run_bot.py`

Esto no es solo una preferencia arquitectonica. En este repo es una restriccion real:

- `run_bot.py` parsea CLI al importar
- mezcla estado global, loops async y wiring del proceso
- varios datos utiles viven solo en memoria del proceso del bot

Conclusion:

- la API nunca debe depender de importar `run_bot.py`
- la integracion correcta es via estado persistido y command bus

### 2. El backend separado no puede confiar en memoria local

Hoy estos estados viven solo en memoria del proceso del bot:

- `_stats`
- `_wallet_sol_balance`
- `_shadow_positions`
- `_pending_ai_vectors`
- `_BUY_LIMITER`
- la cola `_pair_watch` de `utils/lista_pares.py`
- los candidatos y la salud de regimen en `analytics/strategy_runtime.py`
- sombras abiertas y contexto live en `analytics/research_runtime.py`

Eso significa:

- un proceso API separado no ve el estado live real
- Overview, Runtime, Queue y Control no pueden ser fieles sin snapshots persistentes

### 3. La UI debe degradar bien si faltan artefactos

El sistema ya tiene estados incompletos por diseno:

- puede no haber posiciones abiertas
- puede no existir `paper_portfolio.json`
- puede faltar `recommended_threshold.json`
- puede faltar parquet nuevo
- el bot puede estar parado y la UI seguir levantada

La UI debe soportar `empty`, `stale`, `degraded` y `error` como estados de primera clase.

### 4. El control desde UI debe ser indirecto y auditable

No se debe meter logica mutante en la API que toque directamente el loop del bot.

La via correcta es:

1. la UI envia una orden al backend
2. el backend persiste la orden
3. el bot hace polling de esa tabla
4. el bot ejecuta
5. el bot escribe resultado y estado

---

## Principio de arquitectura objetivo

### Procesos

1. Bot runtime
   - discovery
   - filtros
   - scoring
   - buy/sell
   - monitor
   - research lane
   - retrain

2. API backend
   - lee DB, JSONL, parquet, metrics y logs
   - expone REST
   - expone SSE cuando tenga sentido
   - crea y consulta comandos operativos

3. Frontend web
   - consume la API
   - compone vistas operativas
   - dispara acciones seguras

### Contrato de estado

Lectura:

- DB
- JSONL
- parquet
- JSON de metrics
- logs
- snapshots persistidos por el bot

Control:

- tabla de comandos
- tabla o fila de estado runtime
- auditoria de comandos

---

## Fuentes de datos por dominio UI

### Read-only que ya podemos explotar

**Overview**

- `analytics/reporting.py`
- `runtime_events.jsonl`
- `research_scorecard.json`
- `positions`

**Discovery**

- `runtime_events.jsonl`
  - `queue_add`
  - `requeue`
  - `queue_drop`
- `candidate_outcomes.jsonl`
  - `candidate_stage`
  - `candidate_decision`

**Queue**

- `runtime_events.jsonl`
- snapshot persistente futuro del estado de cola

**Positions / Trades**

- `positions`
- `tokens`
- `trade_pnl.py`

**Replay**

- `positions`
- `runtime_events.jsonl`
- `candidate_outcomes.jsonl`
- ultima fila del parquet por `address`

**Analytics**

- `analytics/reporting.summarize_edge()`

**ML**

- `analytics.ai_predict.model_runtime_status()`
- `dataset_quality.json`
- `train_status.json`
- `recommended_threshold.json`
- `research_thresholds.json`

**Config**

- `analytics.reporting.snapshot_effective_config()`
- `analytics.filters.describe_filter_policy()`
- `analytics.sizing.describe_sizing_policy()`
- `analytics.exit_policy.describe_exit_policy()`

**Logs**

- `logs/*.txt`
- `runtime_events.jsonl`
- `candidate_outcomes.jsonl`

### Live que necesitan snapshots nuevos del bot

**Runtime health**

- ultimo loop ok
- ultimo discovery ok
- ultimo monitor ok
- heartbeat
- build/runtime version

**Queue live**

- pending
- requeued
- cooldown
- oldest pending

**Wallet live**

- balance SOL actual en memoria
- ultimo refresh

**ML gate live**

- threshold efectivo aplicado por el bot
- `mode`
- `enforce`
- `activation_ready`

**Strategy live**

- health por regimen
- cooldowns
- success rate de ejecucion
- coverage rate de precio

**Control live**

- discovery paused
- buys paused
- retrain running
- refresh reports running
- ultimo comando ejecutado

---

## Modelo de estado persistente recomendado

### V1 minima y pragmatica

No hace falta un sistema enorme para empezar. Hace falta uno correcto.

Tablas nuevas recomendadas:

#### `bot_runtime_state`

Una fila por bot con el ultimo snapshot vivo.

Campos sugeridos:

- `bot_id`
- `updated_at`
- `heartbeat_at`
- `started_at`
- `dry_run`
- `process_state`
- `discovery_paused`
- `buys_paused`
- `wallet_sol`
- `wallet_checked_at`
- `queue_pending`
- `queue_requeued`
- `queue_cooldown`
- `queue_items_json`
- `buy_limiter_in_window`
- `stats_json`
- `ml_gate_json`
- `strategy_health_json`
- `research_json`
- `last_error`
- `build_info_json`

#### `control_commands`

Tabla append-only para mandar acciones al bot y conservar auditoria basica.

Campos sugeridos:

- `id`
- `bot_id`
- `command_type`
- `payload_json`
- `status`
  - `pending`
  - `running`
  - `done`
  - `failed`
  - `rejected`
- `requested_by`
- `requested_at`
- `started_at`
- `finished_at`
- `result_json`
- `error_text`

#### `ui_saved_views`

Para filtros, layouts y enlaces persistentes de la UI.

Campos sugeridos:

- `id`
- `view_name`
- `page_key`
- `filters_json`
- `layout_json`
- `created_by`
- `created_at`
- `updated_at`

### Para mas adelante

- `alert_rules`
- `alert_events`
- `user_sessions`
- `api_keys_local`

---

## Acciones que la UI debe poder gobernar

### Seguras para V1

Estas son realistas y coherentes con el bot actual:

- `pause_discovery`
- `resume_discovery`
- `pause_buys`
- `resume_buys`
- `reload_model`
- `trigger_retrain`
- `refresh_reports`
- `set_log_level`

### Lo que implica en `run_bot.py`

Para soportarlas bien hay que introducir checks en puntos concretos:

- antes del bloque de discovery Dex
- antes de procesar stream Pump.fun
- antes de validar cola
- justo antes de ejecutar un buy
- en el loop de retrain
- en el refresco de scorecards/reportes

### Lo que NO debe entrar en V1

- editar `.env` desde UI
- mostrar secretos
- reiniciar el proceso desde la web
- firmar transacciones arbitrarias desde la UI
- ejecutar SQL o shell desde frontend

---

## API propuesta

### Stack

- FastAPI
- Uvicorn
- Pydantic
- SQLAlchemy async
- polling primero
- SSE despues donde de verdad aporte

### Estructura recomendada

```text
api/
  main.py
  settings.py
  deps.py
  routes/
    health.py
    overview.py
    runtime.py
    discovery.py
    queue.py
    positions.py
    trades.py
    analytics.py
    ml.py
    config.py
    logs.py
    control.py
  schemas/
  services/
  repositories/
```

### Endpoint groups recomendados

#### Salud y fuentes

- `GET /api/v1/health`
- `GET /api/v1/sources/status`

#### Overview y runtime

- `GET /api/v1/overview`
- `GET /api/v1/runtime/state`
- `GET /api/v1/runtime/events`
- `GET /api/v1/runtime/strategy-health`

#### Discovery y queue

- `GET /api/v1/discovery/feed`
- `GET /api/v1/discovery/summary`
- `GET /api/v1/queue/summary`
- `GET /api/v1/queue/items`

#### Positions y trades

- `GET /api/v1/positions/open`
- `GET /api/v1/trades/closed`
- `GET /api/v1/trades/{trade_id}`
- `GET /api/v1/trades/{trade_id}/replay`

#### Analytics, ML y config

- `GET /api/v1/analytics/edge`
- `GET /api/v1/analytics/baseline`
- `GET /api/v1/ml/status`
- `GET /api/v1/ml/research`
- `GET /api/v1/config/effective`
- `GET /api/v1/config/policies`

#### Logs

- `GET /api/v1/logs/tail`
- `GET /api/v1/events/runtime`
- `GET /api/v1/events/research`

#### Control

- `GET /api/v1/control/state`
- `GET /api/v1/control/commands`
- `POST /api/v1/control/commands`

### Politica de frescura recomendada

- `overview`: 5 s
- `runtime/state`: 3 s
- `discovery/feed`: 5 s
- `queue/summary`: 3 s
- `positions/open`: 5 s
- `logs/events`: 2 s
- `analytics/edge`: 60 s
- `ml/status`: 15 s
- `config`: 60 s

---

## Frontend propuesto

### Stack

- React
- TypeScript
- Vite
- React Router
- TanStack Query
- TanStack Table
- ECharts

### Paginas por prioridad real

| Pagina | Prioridad | Estado actual de datos |
| --- | --- | --- |
| Overview | alta | viable tras `bot_runtime_state` |
| Runtime | alta | viable tras `bot_runtime_state` |
| Discovery | alta | viable ya con JSONL |
| Queue | alta | necesita snapshot de cola |
| Positions | alta | viable ya con SQLite cuando haya filas |
| Trades | alta | viable ya con SQLite cuando haya filas |
| Trade Replay | alta | viable al unir SQLite + JSONL + parquet |
| Analytics | media | viable ya con `analytics/reporting.py` |
| ML Center | media | viable ya con metrics JSON + meta |
| Config Center | media | viable ya con helpers de config/policy |
| Logs and Events | media | viable ya con logs + JSONL |
| Control Center | alta | necesita `control_commands` |
| Wallet | media | necesita snapshot runtime para ser fiable |
| Alerts | baja | requiere motor nuevo |
| Admin | baja | puede salir al final |

### Regla UX por pagina

Cada pagina debe resolver una decision operativa, no solo mostrar datos.

Patron base:

1. resumen arriba
2. vista principal abajo
3. detalle lateral o drawer
4. estados `loading`, `empty`, `stale`, `degraded`, `error`

---

## Visual charter

### Direccion elegida

**Industrial editorial operations desk**

La app debe sentirse:

- sobria
- precisa
- densa pero legible
- fuerte visualmente sin parecer un exchange retail

### Tipografia

- display: `Sora`
- cuerpo: `Source Sans 3`
- datos: `IBM Plex Mono`

### Color

- neutrales frios con tinte petroleo u oliva mineral
- acento principal medido
- nada de neon crypto
- nada de negro puro y blanco puro

### Layout

- sidebar fija
- topbar util
- canvas editorial asimetrico
- tablas y timelines como piezas nobles

### Anti-patrones prohibidos

- admin template generica
- grid infinita de metric cards iguales
- glassmorphism
- glow gratuito
- charts decorativos
- texto gradiente
- cards dentro de cards

---

## Fases reales de implementacion

## Fase A - Observabilidad read-only

Objetivo:

- levantar API y frontend sin tocar el comportamiento operativo del bot

Incluye:

- backend HTTP
- lectura de DB, JSONL, parquet, logs y metrics JSON
- pages de overview parcial, discovery, analytics, ML, config y logs

## Fase B - Estado live publicable

Objetivo:

- hacer visible el estado que hoy solo existe en memoria

Incluye:

- `bot_runtime_state`
- publisher de heartbeat
- snapshot de queue, wallet, ml gate y strategy health

## Fase C - Control seguro

Objetivo:

- gestionar el bot desde la UI sin acoplar API y runtime

Incluye:

- `control_commands`
- polling de comandos en el bot
- pause/resume
- reload model
- trigger retrain
- refresh reports

## Fase D - Cierre operacional

Objetivo:

- cerrar auth local, packaging, alertas y calidad final

---

## Plan completo de PRs

## PR-UI-1 - Contratos y inventario

### Objetivo

Congelar el contrato real entre bot, API y frontend.

### Entregables

- `docs/API_UI_SPEC.md`
- `docs/UI_STATE_CONTRACT.md`
- `docs/UI_SITEMAP.md`
- `docs/UI_VISUAL_CHARTER.md`

### Debe cerrar

- fuentes de verdad por pagina
- endpoints iniciales
- payloads
- estados UI
- acciones mutantes permitidas
- restricciones de seguridad

### Criterio de salida

Ninguna pantalla depende de memoria del bot sin que eso este explicitado.

---

## PR-UI-2 - Backend skeleton desacoplado

### Objetivo

Levantar FastAPI sin importar `run_bot.py`.

### Archivos objetivo

- `api/main.py`
- `api/settings.py`
- `api/routes/*`
- `api/repositories/*`
- `api/services/*`

### Criterio de salida

- smoke API
- health endpoint
- lectura basica de DB y metrics

---

## PR-UI-3 - Runtime state publisher

### Objetivo

Persistir el estado live minimo que hoy esta solo en memoria.

### Archivos objetivo

- nuevo modulo, por ejemplo:
  - `runtime/state_publisher.py`
  - `runtime/state_models.py`
- cambios en:
  - `run_bot.py`
  - `db/models.py`
  - `db/database.py`

### Debe publicar

- heartbeat
- dry-run / process_state
- queue stats
- wallet snapshot
- stats del funnel
- ml gate efectivo
- health por regimen
- flags de pausa

### Criterio de salida

Una API separada ya puede saber si el bot esta vivo, si la cola crece y si los buys estan pausados.

Nota:

- el snapshot de cola para UI puede vivir inicialmente en `bot_runtime_state.queue_items_json`
- no hace falta bloquear `PR-UI-3` con una tabla extra de cola si el JSON persistido cubre la inspeccion operativa v1

---

## PR-UI-4 - API de overview y runtime

### Objetivo

Exponer la consola base del sistema.

### Endpoints minimos

- `GET /api/v1/overview`
- `GET /api/v1/runtime/state`
- `GET /api/v1/runtime/events`
- `GET /api/v1/runtime/strategy-health`

### Criterio de salida

Overview y Runtime se pueden construir sin leer logs manualmente.

---

## PR-UI-5 - API de discovery, queue y logs

### Objetivo

Cerrar el funnel pre-buy.

### Endpoints minimos

- `GET /api/v1/discovery/feed`
- `GET /api/v1/discovery/summary`
- `GET /api/v1/queue/summary`
- `GET /api/v1/queue/items`
- `GET /api/v1/logs/tail`

### Criterio de salida

Se puede ver donde cae el ruido y donde se atasca la cola.

---

## PR-UI-6 - API de positions, trades y replay

### Objetivo

Construir la parte con mas valor operativo.

### Endpoints minimos

- `GET /api/v1/positions/open`
- `GET /api/v1/trades/closed`
- `GET /api/v1/trades/{trade_id}`
- `GET /api/v1/trades/{trade_id}/replay`

### Nota

El replay debe unir:

- fila de `positions`
- contexto de `tokens`
- eventos de `runtime_events.jsonl`
- eventos de `candidate_outcomes.jsonl`
- snapshot T0 del parquet

### Criterio de salida

Un trade se reconstruye completo sin abrir SQLite a mano.

---

## PR-UI-7 - API de analytics, ML y config

### Objetivo

Cerrar la capa read-only profunda.

### Endpoints minimos

- `GET /api/v1/analytics/edge`
- `GET /api/v1/analytics/baseline`
- `GET /api/v1/ml/status`
- `GET /api/v1/ml/research`
- `GET /api/v1/config/effective`
- `GET /api/v1/config/policies`

### Criterio de salida

La UI ya puede explicar edge, ML y politica efectiva sin terminal.

---

## PR-UI-8 - Frontend shell y design system

### Objetivo

Montar la base visual y de navegacion.

### Entregables

- router
- sidebar
- topbar
- data table kit
- chart shell
- drawer system
- theme tokens
- typography tokens
- motion tokens

### Criterio de salida

La shell ya parece una consola operacional, no una plantilla.

---

## PR-UI-9 - Pages: Overview y Runtime

### Objetivo

Primera experiencia de uso diario.

### Debe incluir

- hero de estado
- resumen de queue, wallet y funnel
- runtime pulse
- strategy health

---

## PR-UI-10 - Pages: Discovery, Queue y Logs

### Objetivo

Hacer visible el embudo previo a la compra.

### Debe incluir

- feed de decisiones
- breakdown por reason
- requeues
- cola actual
- log tail

---

## PR-UI-11 - Pages: Positions, Trades y Replay

### Objetivo

Cerrar el circuito de auditoria operativa.

### Prioridad

Muy alta.

---

## PR-UI-12 - Pages: Analytics, ML y Config

### Objetivo

Dar visibilidad profunda a edge, dataset y politica efectiva.

---

## PR-UI-13 - Command bus backend

### Objetivo

Preparar gestion segura del bot desde la UI.

### Archivos objetivo

- nuevo modulo, por ejemplo:
  - `runtime/command_bus.py`
- cambios en:
  - `run_bot.py`
  - `db/models.py`
  - `db/database.py`
  - `api/routes/control.py`

### Comandos V1

- `pause_discovery`
- `resume_discovery`
- `pause_buys`
- `resume_buys`
- `reload_model`
- `trigger_retrain`
- `refresh_reports`
- `set_log_level`

### Criterio de salida

Los comandos quedan persistidos, ejecutados por el bot y trazados con estado final.

---

## PR-UI-14 - Control page

### Objetivo

Exponer las acciones operativas desde la UI.

### Debe incluir

- command forms
- confirmaciones
- historial
- estado actual del bot

---

## PR-UI-15 - Auth local, roles y saved views

### Objetivo

Cerrar el uso real en entorno local o semi-controlado.

### Alcance

- login local simple
- roles `viewer`, `operator`, `admin`
- permisos por accion
- `ui_saved_views`

### Nota

Si se necesita llegar antes a la gestion via UI en localhost, se puede usar una fase transitoria sin auth solo en `127.0.0.1`, pero debe quedar explicitamente marcada como modo dev.

---

## PR-UI-16 - Packaging, arranque y quality gates

### Objetivo

Dejar la plataforma lista para uso diario.

### Entregables

- scripts de arranque bot + api + frontend
- build frontend
- smoke scripts
- docs de operacion
- backup / restore basico

---

## Orden recomendado real

1. PR-UI-1
2. PR-UI-2
3. PR-UI-3
4. PR-UI-4
5. PR-UI-5
6. PR-UI-6
7. PR-UI-7
8. PR-UI-8
9. PR-UI-9
10. PR-UI-10
11. PR-UI-11
12. PR-UI-12
13. PR-UI-13
14. PR-UI-14
15. PR-UI-15
16. PR-UI-16

Motivo:

- primero separar contratos y estado
- despues exponer lectura
- luego montar shell y paginas
- despues control plane
- al final auth y packaging

---

## Definition of done

La UI se considerara lograda cuando se cumpla todo esto:

1. El operador sabe en menos de 10 segundos si el bot esta vivo, degradado o parado.
2. La UI funciona aunque el bot este caido.
3. Overview y Queue se alimentan de snapshots persistidos, no de memoria local del backend.
4. Un trade se puede reconstruir completo sin abrir SQLite ni logs manuales.
5. La politica efectiva de filtros, sizing, exits y ML es visible en UI.
6. El operador puede pausar discovery y buys desde la UI con auditoria.
7. El operador puede recargar modelo, lanzar retrain y refrescar reportes desde la UI.
8. La UI no parece un panel crypto generico ni una plantilla admin.
9. Si la UI o la API caen, el bot sigue intacto.

---

## Instrucciones de arranque para la implementacion

### Entorno obligatorio

Usar siempre la venv del proyecto:

```powershell
.\.venv\Scripts\python.exe scripts\runtime_smoke.py
```

Motivo:

- el Python global actual falla con binarios de `numpy/pandas/pyarrow`
- `runtime_smoke.py` si pasa correctamente con la venv del proyecto

### Regla de trabajo

- no usar `python` del sistema
- usar `.\.venv\Scripts\python.exe`
- no importar `run_bot.py` desde la API
- no leer `docs/*.md` como fuente de verdad
- todo estado live que la UI necesite debe persistirse antes

### Siguiente paso correcto

Empezar por **PR-UI-1**.

Ese PR debe cerrar cuatro cosas antes de tocar frontend:

1. contrato de estado persistente del bot
2. contrato HTTP de lectura
3. mapa de paginas y decisiones
4. charter visual resumido

Sin eso, la UI volveria a ser bonita pero conceptualmente falsa.
