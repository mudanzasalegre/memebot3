# UI Sitemap

## Objetivo

Definir la navegacion, las paginas y la decision operativa que resuelve cada pantalla.

La UI no se organiza por tecnologia ni por tablas. Se organiza por preguntas del operador.

## App shell

### Estructura global

- sidebar fija a la izquierda
- topbar compacta con estado global
- canvas principal con layout editorial
- drawer lateral para detalle contextual

### Elementos globales

- `bot status chip`: `running`, `degraded`, `stopped`, `stale`
- `source health strip`: DB, runtime events, research events, runtime_state
- `command quick actions`: acceso rapido a `pause/resume`
- `time range switcher`: donde tenga sentido
- `search omnibox`: `trade_id`, `address`, `symbol`

## Rutas v1

| Ruta | Pagina | Prioridad | Estado de datos |
| --- | --- | --- | --- |
| `/overview` | Overview | alta | parcial antes de `bot_runtime_state`, completa despues |
| `/runtime` | Runtime | alta | necesita `bot_runtime_state` |
| `/discovery` | Discovery | alta | viable ya con JSONL |
| `/queue` | Queue | alta | necesita `queue_items_json` |
| `/positions` | Positions | alta | viable ya con SQLite |
| `/trades` | Trades | alta | viable ya con SQLite |
| `/trades/:tradeId` | Trade Replay | alta | viable con DB + JSONL + parquet |
| `/analytics` | Analytics | media | viable ya |
| `/ml` | ML Center | media | viable ya |
| `/config` | Config Center | media | viable ya |
| `/logs` | Logs and Events | media | viable ya |
| `/control` | Control Center | alta | necesita `control_commands` |

## Navegacion recomendada

### Group: Monitor

- Overview
- Runtime
- Discovery
- Queue

### Group: Inspect

- Positions
- Trades
- Analytics
- ML Center
- Config Center
- Logs and Events

### Group: Operate

- Control Center

## Paginas y decisiones

### Overview

Decision principal:

- "Esta vivo el sistema y merece que siga mirando o intervenir ya?"

Debe mostrar:

- hero de estado del bot
- resumen de queue, wallet, ML y research
- resumen de posiciones y PnL
- alertas de staleness y fuentes degradadas

Endpoints:

- `GET /api/v1/overview`

Drill-downs:

- click a `queue` -> `/queue`
- click a `positions` -> `/positions`
- click a `ml` -> `/ml`
- click a `runtime` -> `/runtime`

### Runtime

Decision principal:

- "Que parte del runtime esta fallando o frenada?"

Debe mostrar:

- heartbeat
- flags `discovery_paused` y `buys_paused`
- `retrain_state` y `reports_refresh_state`
- strategy health por regimen
- buy limiter
- ultimo error

Endpoints:

- `GET /api/v1/runtime/state`
- `GET /api/v1/runtime/strategy-health`
- `GET /api/v1/runtime/events`

### Discovery

Decision principal:

- "Donde se esta cayendo el funnel antes de comprar?"

Debe mostrar:

- feed unificado del funnel
- breakdown por `stage`, `decision_action` y `reason`
- filtros por `address`, `regime`, `reason`
- foco en `rejected`, `wait`, `shadow`, `bought`

Endpoints:

- `GET /api/v1/discovery/feed`
- `GET /api/v1/discovery/summary`

### Queue

Decision principal:

- "Que hay en cola ahora mismo, que esta esperando y por que?"

Debe mostrar:

- resumen `pending`, `requeued`, `cooldown`
- tabla de items live
- columna `attempts`, `retries_left`, `next_retry_at`, `last_reason`
- edad de cada item y elemento mas viejo

Endpoints:

- `GET /api/v1/queue/summary`
- `GET /api/v1/queue/items`

Notas:

- sin `queue_items_json`, la pagina debe existir pero declararse `degraded`

### Positions

Decision principal:

- "Tengo riesgo abierto ahora mismo?"

Debe mostrar:

- tabla de posiciones abiertas
- PnL live disponible
- size, regime, buy amount y peak
- accesos al replay por `trade_id`

Endpoints:

- `GET /api/v1/positions/open`

### Trades

Decision principal:

- "Como ha cerrado historicamente el sistema y donde se esta ganando o perdiendo?"

Debe mostrar:

- tabla de trades cerrados
- filtros por `exit_reason`, `entry_regime`, `outcome`
- orden por `closed_at`
- resumen superior de win rate y avg pnl

Endpoints:

- `GET /api/v1/trades/closed`

### Trade Replay

Decision principal:

- "Que paso exactamente desde T0 hasta el cierre de este trade?"

Debe mostrar:

- ficha del trade
- metadata del token
- snapshot T0 del parquet
- timeline runtime
- timeline research
- duraciones derivadas

Endpoints:

- `GET /api/v1/trades/{trade_id}`
- `GET /api/v1/trades/{trade_id}/replay`

### Analytics

Decision principal:

- "Donde esta el edge real y como se distribuye por regimen, sizing y exits?"

Debe mostrar:

- `overview` de `summarize_edge()`
- breakdowns por `exit_reason`, `entry_regime`, `size_bucket`, `price_source`
- coverage de features

Endpoints:

- `GET /api/v1/analytics/edge`
- `GET /api/v1/analytics/baseline`

### ML Center

Decision principal:

- "Esta el modelo usable, activable y coherente con el runtime?"

Debe mostrar:

- model runtime status
- threshold recomendado
- dataset quality
- research scorecard
- thresholds por regimen

Endpoints:

- `GET /api/v1/ml/status`
- `GET /api/v1/ml/research`

### Config Center

Decision principal:

- "Que politica efectiva esta corriendo de verdad?"

Debe mostrar:

- config efectiva
- policy de filtros
- policy de sizing
- policy de exits
- strategy policy por regimen

Endpoints:

- `GET /api/v1/config/effective`
- `GET /api/v1/config/policies`

### Logs and Events

Decision principal:

- "Que ha pasado en bruto en el sistema?"

Debe mostrar:

- tail de logs de app
- feeds raw-normalized de runtime y research
- filtros por `event_type`, `address`, `reason`

Endpoints:

- `GET /api/v1/logs/tail`
- `GET /api/v1/events/runtime`
- `GET /api/v1/events/research`

### Control Center

Decision principal:

- "Que puedo operar desde aqui y con que auditoria?"

Debe mostrar:

- estado actual del bot y flags
- acciones soportadas
- confirmacion previa
- historial de comandos
- resultado y error por comando

Endpoints:

- `GET /api/v1/control/state`
- `GET /api/v1/control/commands`
- `POST /api/v1/control/commands`

Notas:

- la pagina no sale completa hasta `PR-UI-13`
- nunca ejecuta side effects directos

## Cross-page navigation

- click en `address` desde Discovery, Queue o Logs abre el contexto filtrado de ese token
- click en `trade_id` abre `/trades/:tradeId`
- click en `reason` o `stage` abre la tabla ya filtrada
- click en `regime` abre Analytics o Runtime segun contexto

## Estados globales de UI

Cada pagina debe implementar los cinco estados:

### `loading`

- skeletons
- no spinner central infinito salvo primer boot muy corto

### `empty`

- mensaje contextual
- CTA de navegacion valida

### `stale`

- banner visible con timestamp exacto
- los datos se siguen leyendo, pero no se venden como live

### `degraded`

- bloque afectado marcado, no caida de toda la pagina si el resto sigue util

### `error`

- error explicito con source afectada
- posibilidad de retry

## Lanzamiento por fases

### Fase 1: read-only util

Paginas que deben quedar ya en cuanto exista la API skeleton:

- Discovery
- Analytics
- ML Center
- Config Center
- Logs and Events

### Fase 2: runtime live fiel

Paginas que dependen de `bot_runtime_state`:

- Overview
- Runtime
- Queue

### Fase 3: control seguro

Pagina que depende de `control_commands`:

- Control Center

## Criterios de calidad de la navegacion

- cada pagina responde una pregunta operativa clara
- ninguna pagina existe solo para mostrar tarjetas
- el operador puede saltar de resumen a detalle en un click
- cualquier estado `stale` o `degraded` queda visible sin abrir devtools
