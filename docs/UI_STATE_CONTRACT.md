# UI State Contract

## Objetivo

Congelar el contrato de estado entre bot runtime, backend API y frontend UI para que:

- la API lea solo artefactos persistidos
- la UI no dependa de memoria privada del proceso del bot
- las acciones mutantes pasen por un command bus auditable

Este documento es normativo para `PR-UI-2` y `PR-UI-3`.

## Principios

1. `run_bot.py` no se importa desde la API.
2. `docs/*.md` nunca son source of truth para la UI.
3. El bot es el unico writer de estado runtime live.
4. La API puede normalizar y agregar, pero no inventar estado.
5. Toda accion mutante se persiste antes de ejecutarse.
6. Todo estado de UI debe poder caer en `fresh`, `stale`, `degraded`, `empty` o `error`.

## Identidades canonicas

- `bot_id`: identificador del runtime. En v1 local sera `"main"`.
- `token_address`: `tokens.address`.
- `trade_id`: `positions.id`.
- `runtime_event_id`: derivado de `ts_utc + event_type + address + line_number`.
- `research_event_id`: derivado de `ts_utc + event_type + address + line_number`.
- `saved_view_id`: `ui_saved_views.id`.
- `command_id`: `control_commands.id`.

## Sources of truth

| Dominio | Source of truth | Writer actual | Reader principal | Observaciones |
| --- | --- | --- | --- | --- |
| Catalogo de tokens | `tokens` | bot | API | tabla canonical para metadata y discovery |
| Posiciones y trades | `positions` | bot | API | `trade_id = positions.id` |
| Revivals | `revived_tokens` | bot | API | opcional para drill-down |
| Runtime event feed | `data/metrics/runtime_events.jsonl` | bot | API | append-only |
| Research event feed | `data/metrics/candidate_outcomes.jsonl` | bot | API | append-only |
| Edge y baseline read-only | DB + parquet + JSONL via `analytics/reporting.py` | bot genera datos, API agrega | API | no leer `docs/EDGE_REPORT.md` ni `docs/BASELINE.md` |
| ML runtime meta | `ml/model.pkl`, `ml/model.meta.json`, `data/metrics/*.json` | train/retrain + bot | API | algunos ficheros son opcionales |
| Research scorecard | `data/metrics/research_scorecard.json` y `research_thresholds.json` | bot/research lane | API | puede quedar stale respecto al JSONL |
| Paper mode | `data/paper_portfolio.json` | bot | API | opcional |
| Logs | `logs/*.txt` | bot | API | solo lectura, rutas whitelisted |
| Runtime live consolidado | `bot_runtime_state` | bot | API | nuevo en `PR-UI-3` |
| Command bus | `control_commands` | API crea, bot ejecuta | API + bot | nuevo en `PR-UI-13` |
| Saved views | `ui_saved_views` | API/UI | API/UI | nuevo en `PR-UI-15` |

## Estado persistido que falta

Hoy la UI no puede representar de forma fiable estos dominios sin nuevo estado persistido:

- heartbeat del bot
- flags operativos (`discovery_paused`, `buys_paused`)
- wallet live
- queue live
- buy limiter live
- strategy health consolidado
- research live consolidado
- ultimo error operacional
- historial de comandos

## Tabla `bot_runtime_state`

Una fila por `bot_id`. El bot hace `upsert`. La API nunca escribe aqui.

### Semantica general

- cardinalidad: `1 row per bot_id`
- ownership: bot runtime
- write pattern: `upsert`
- expected cadence: cada `5s` o antes si cambia algo relevante
- retention: ultima fila viva, sin historico en esta tabla

### Columnas requeridas

| Columna | Tipo sugerido | Obligatoria | Descripcion |
| --- | --- | --- | --- |
| `bot_id` | `TEXT PK` | si | `"main"` en v1 |
| `updated_at` | `TIMESTAMPTZ` | si | instante del ultimo snapshot persistido |
| `heartbeat_at` | `TIMESTAMPTZ` | si | ultimo latido del proceso |
| `started_at` | `TIMESTAMPTZ` | no | inicio del proceso actual |
| `process_state` | `TEXT` | si | `starting`, `running`, `degraded`, `stopping`, `stopped` |
| `dry_run` | `BOOLEAN` | si | copia de `CFG.DRY_RUN` |
| `discovery_paused` | `BOOLEAN` | si | flag gobernable por command bus |
| `buys_paused` | `BOOLEAN` | si | flag gobernable por command bus |
| `retrain_state` | `TEXT` | si | `idle`, `running`, `failed` |
| `reports_refresh_state` | `TEXT` | si | `idle`, `running`, `failed` |
| `wallet_sol` | `REAL` | no | ultimo balance visible por el bot |
| `wallet_checked_at` | `TIMESTAMPTZ` | no | timestamp del ultimo refresh wallet |
| `open_positions_count` | `INTEGER` | si | conteo live del runtime |
| `queue_pending` | `INTEGER` | si | elementos pendientes |
| `queue_requeued` | `INTEGER` | si | elementos actualmente reencolados |
| `queue_cooldown` | `INTEGER` | si | elementos esperando backoff |
| `queue_oldest_first_seen_at` | `TIMESTAMPTZ` | no | antiguedad del item mas viejo |
| `buy_limiter_in_window` | `INTEGER` | si | compras dentro de la ventana activa |
| `buy_limiter_window_s` | `INTEGER` | si | tamano de ventana del limiter |
| `discovery_last_ok_at` | `TIMESTAMPTZ` | no | ultimo ciclo discovery sano |
| `monitor_last_ok_at` | `TIMESTAMPTZ` | no | ultimo ciclo monitor sano |
| `last_error` | `TEXT` | no | error operativo resumido |
| `last_error_at` | `TIMESTAMPTZ` | no | timestamp del ultimo error |
| `stats_json` | `TEXT JSON` | si | counters normalizados del funnel |
| `ml_gate_json` | `TEXT JSON` | si | estado efectivo del gate ML |
| `strategy_health_json` | `TEXT JSON` | si | salida consolidada por regimen |
| `research_json` | `TEXT JSON` | si | estado live de research lane |
| `queue_items_json` | `TEXT JSON` | si | snapshot de items de cola para UI |
| `build_info_json` | `TEXT JSON` | si | versionado del proceso y del entorno |

### JSON subcontracts

#### `stats_json`

Contrato minimo:

```json
{
  "queue_added_total": 0,
  "queue_requeued_total": 0,
  "queue_dropped_total": 0,
  "buys_total": 0,
  "sells_total": 0,
  "errors_total": 0,
  "last_buy_at": null,
  "last_sell_at": null
}
```

#### `ml_gate_json`

Debe ser coherente con `analytics.ai_predict.model_runtime_status()` y con el gate efectivo aplicado por el bot:

```json
{
  "mode": "shadow",
  "enforced": false,
  "threshold": 0.0,
  "activation_ready": false,
  "dataset_quality_passed": null,
  "model_loaded": true,
  "model_exists": true,
  "meta_exists": true,
  "features_count": 42,
  "last_reload_at": null,
  "last_decision_at": null
}
```

`mode` debe transportar el valor efectivo real del bot. En el runtime actual los valores validos son `legacy`, `shadow`, `enforce` u `off`.

#### `strategy_health_json`

Debe seguir la forma de `analytics.strategy_runtime.describe_regime_health()`:

```json
{
  "pump_early": {
    "requested_mode": "shadow",
    "health_state": "normal",
    "trade_count": 0,
    "avg_pnl_pct": null,
    "win_rate": null,
    "exec_rate": null,
    "price_rate": null,
    "consecutive_losses": 0,
    "cooldown_until": null,
    "disable_reason": null
  },
  "dex_mature": {},
  "revival": {}
}
```

#### `research_json`

Contrato minimo:

```json
{
  "lane_enabled": true,
  "shadow_enabled": true,
  "open_shadow_count": 0,
  "open_shadow_by_regime": {
    "pump_early": 0,
    "dex_mature": 0,
    "revival": 0
  },
  "scorecard_generated_at": null,
  "thresholds_generated_at": null,
  "last_event_at": null
}
```

#### `queue_items_json`

Este campo existe porque `bot_runtime_state` por si sola no basta para construir la pagina Queue. El snapshot debe contener como minimo los items pendientes y en cooldown que la UI necesita inspeccionar.

```json
{
  "captured_at": "2026-03-31T10:30:25.788966+00:00",
  "items": [
    {
      "address": "9oSffRHv2y3reTkCfgzuM93ugLqC5ZaTY57xyKEfpump",
      "symbol": null,
      "status": "cooldown",
      "discovered_via": "dex",
      "entry_regime": "dex_mature",
      "first_seen_at": "2026-03-31T10:15:59.447187+00:00",
      "attempts": 3,
      "retries_left": 2,
      "next_retry_at": "2026-03-31T10:32:56.697731+00:00",
      "last_reason": "other",
      "queue_age_minutes": 13.95
    }
  ]
}
```

#### `build_info_json`

Contrato minimo:

```json
{
  "app": "memebot3",
  "bot_version": "local",
  "python_version": "3.12.3",
  "pid": 12345,
  "hostname": "workstation",
  "git_sha": null
}
```

## Tabla `control_commands`

Tabla append-only. La API inserta. El bot hace polling, marca progreso y escribe resultado.

### Columnas requeridas

| Columna | Tipo sugerido | Obligatoria | Descripcion |
| --- | --- | --- | --- |
| `id` | `INTEGER PK` | si | command id |
| `bot_id` | `TEXT` | si | destino del comando |
| `command_type` | `TEXT` | si | enum de acciones soportadas |
| `payload_json` | `TEXT JSON` | si | payload validado por API |
| `status` | `TEXT` | si | `pending`, `running`, `done`, `failed`, `rejected`, `cancelled` |
| `requested_by` | `TEXT` | si | identidad de operador o header tecnico |
| `requested_from` | `TEXT` | no | `ui`, `api`, `cli` |
| `idempotency_key` | `TEXT` | no | para evitar dobles submits |
| `requested_at` | `TIMESTAMPTZ` | si | insercion |
| `started_at` | `TIMESTAMPTZ` | no | inicio real de ejecucion |
| `finished_at` | `TIMESTAMPTZ` | no | fin real |
| `result_json` | `TEXT JSON` | no | resultado estructurado |
| `error_text` | `TEXT` | no | detalle resumido de error |

### Command types v1

- `pause_discovery`
- `resume_discovery`
- `pause_buys`
- `resume_buys`
- `reload_model`
- `trigger_retrain`
- `refresh_reports`
- `set_log_level`

### Payload contract v1

| `command_type` | Payload |
| --- | --- |
| `pause_discovery` | `{}` |
| `resume_discovery` | `{}` |
| `pause_buys` | `{}` |
| `resume_buys` | `{}` |
| `reload_model` | `{}` |
| `trigger_retrain` | `{"force": false}` |
| `refresh_reports` | `{"force": true, "include": ["baseline", "edge", "research"]}` |
| `set_log_level` | `{"level": "INFO", "logger": "root"}` |

### Lifecycle

1. API inserta fila `pending`.
2. Bot selecciona siguiente fila compatible para su `bot_id`.
3. Bot marca `running`.
4. Bot ejecuta.
5. Bot marca `done`, `failed` o `rejected`.
6. API muestra historial, duracion y resultado.

### Reglas operativas

- la API no ejecuta side effects del comando
- el bot puede rechazar comandos incompatibles con su estado actual
- comandos ya redundantes deben acabar en `rejected`, no en silencio
- `idempotency_key` debe impedir doble submit accidental desde UI

## Tabla `ui_saved_views`

No bloquea `PR-UI-2`, pero el contrato queda congelado aqui.

| Columna | Tipo sugerido | Obligatoria | Descripcion |
| --- | --- | --- | --- |
| `id` | `INTEGER PK` | si | view id |
| `page_key` | `TEXT` | si | ruta logica (`trades`, `discovery`, etc.) |
| `view_name` | `TEXT` | si | nombre visible |
| `filters_json` | `TEXT JSON` | si | filtros serializados |
| `layout_json` | `TEXT JSON` | no | columnas, densidad, paneles |
| `created_by` | `TEXT` | si | owner |
| `created_at` | `TIMESTAMPTZ` | si | auditoria |
| `updated_at` | `TIMESTAMPTZ` | si | auditoria |

## Freshness y staleness

Las paginas no deben inferir salud solo por HTTP 200. Deben mirar frescura real.

### Reglas para `bot_runtime_state`

- `fresh`: `updated_at <= 15s`
- `stale`: `15s < updated_at <= 60s`
- `degraded`: `60s < updated_at <= 180s` o `last_error` presente
- `error`: `updated_at > 180s` o fila ausente cuando deberia existir

### Reglas para artefactos read-only

- `research_scorecard.json` se considera `stale` si su `generated_at_utc` es mas antigua que el ultimo `candidate_outcomes.jsonl`
- `recommended_threshold.json`, `train_status.json`, `dataset_quality.json` pueden faltar sin romper la pagina ML
- `paper_portfolio.json` puede faltar sin romper Overview
- `positions` vacia es `empty`, no `error`

## Cadencia de publicacion recomendada

| Dominio | Writer | Cadencia |
| --- | --- | --- |
| heartbeat y flags | bot | cada `5s` |
| queue counts e items | bot | al mutar cola o max `5s` |
| wallet snapshot | bot | cada `30s` o al detectar cambio |
| strategy health | bot | al cerrar trade, al actualizar cobertura y max `15s` |
| research live | bot | al abrir/cerrar shadow y max `30s` |
| command polling | bot | cada `1s` a `2s` |

## Ownership matrix

| Campo o tabla | Writer | Reader |
| --- | --- | --- |
| `tokens`, `positions`, `revived_tokens` | bot | API |
| `runtime_events.jsonl` | bot | API |
| `candidate_outcomes.jsonl` | bot | API |
| `bot_runtime_state` | bot | API |
| `control_commands` | API crea, bot actualiza | API + bot |
| `ui_saved_views` | API/UI | API/UI |

## Out of scope de este contrato

- auth final y sesiones
- restart del proceso desde navegador
- edicion de `.env`
- ejecucion de shell o SQL desde UI
- streaming bidireccional

## Consecuencia para `PR-UI-2` y `PR-UI-3`

- `PR-UI-2` puede exponer lo read-only ya disponible.
- `PR-UI-3` debe implementar `bot_runtime_state` con este contrato.
- Queue y Control no se consideran fieles hasta que exista este estado persistido.
