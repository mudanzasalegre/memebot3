# API UI Spec

## Objetivo

Definir el contrato HTTP inicial entre backend API y frontend UI para `PR-UI-2` a `PR-UI-7`.

La API:

- lee SQLite, JSONL, parquet, metrics JSON y logs
- normaliza respuesta para la UI
- nunca importa `run_bot.py`
- nunca usa `docs/*.md` como data source

## Stack y versionado

- framework: FastAPI
- base path: `/api/v1`
- transporte inicial: REST + polling
- SSE: fuera de `PR-UI-1`, opcional a partir de `PR-UI-9`

## Convenciones globales

### Envelope de respuesta

Todos los endpoints devuelven:

```json
{
  "data": {},
  "meta": {
    "generated_at": "2026-03-31T10:30:25.900000+00:00",
    "degraded": false,
    "empty": false,
    "stale": false,
    "source_status": []
  }
}
```

### `source_status`

Cada endpoint debe declarar de donde sale la informacion:

```json
[
  {
    "source_key": "sqlite.positions",
    "kind": "sqlite",
    "status": "ok",
    "updated_at": null,
    "detail": "rows=0"
  },
  {
    "source_key": "metrics.runtime_events",
    "kind": "jsonl",
    "status": "ok",
    "updated_at": "2026-03-31T10:30:25.788966+00:00",
    "detail": "append_only"
  }
]
```

Valores de `status`:

- `ok`
- `empty`
- `stale`
- `missing`
- `error`

### List endpoints

Convenciones comunes:

- `limit`: default `50`, max `200`
- `before_ts`: cursor temporal descendente
- `address`: filtro por `token_address`
- `trade_id`: entero cuando aplique
- sort default: `ts desc`

### Error model

- `200`: respuesta valida, aunque venga `degraded` o `empty`
- `202`: comando aceptado y persistido
- `404`: recurso inexistente
- `409`: conflicto de estado o comando redundante
- `422`: payload invalido
- `503`: dependencia critica no disponible

## Endpoint catalog

| Endpoint | Proposito | Sources | Refresh sugerido |
| --- | --- | --- | --- |
| `GET /health` | salud del backend | proceso API | `5s` |
| `GET /sources/status` | salud de ficheros y DB | DB + files | `15s` |
| `GET /overview` | consola resumen | `bot_runtime_state`, DB, metrics JSON | `5s` |
| `GET /runtime/state` | snapshot live del bot | `bot_runtime_state` | `3s` |
| `GET /runtime/events` | timeline runtime normalizada | `runtime_events.jsonl` | `2s` |
| `GET /runtime/strategy-health` | salud por regimen | `bot_runtime_state` o `runtime_events.jsonl` | `3s` |
| `GET /discovery/feed` | feed del funnel discovery | `candidate_outcomes.jsonl`, `runtime_events.jsonl` | `5s` |
| `GET /discovery/summary` | agregados por stage/reason | `candidate_outcomes.jsonl`, `runtime_events.jsonl` | `5s` |
| `GET /queue/summary` | estado actual de cola | `bot_runtime_state`, `runtime_events.jsonl` | `3s` |
| `GET /queue/items` | items de cola live | `bot_runtime_state.queue_items_json` | `3s` |
| `GET /positions/open` | posiciones abiertas | `positions`, `tokens` | `5s` |
| `GET /trades/closed` | tabla de trades cerrados | `positions`, `tokens` | `5s` |
| `GET /trades/{trade_id}` | ficha de trade | `positions`, `tokens` | `5s` |
| `GET /trades/{trade_id}/replay` | replay T0 -> cierre | DB + JSONL + parquet | `5s` |
| `GET /analytics/edge` | resumen edge | `analytics/reporting.py` | `60s` |
| `GET /analytics/baseline` | baseline proyecto | `analytics/reporting.py` | `60s` |
| `GET /ml/status` | estado ML operativo | model/meta + metrics JSON | `15s` |
| `GET /ml/research` | research scorecard y thresholds | `research_scorecard.json`, `research_thresholds.json`, JSONL | `15s` |
| `GET /config/effective` | config efectiva | `analytics/reporting.snapshot_effective_config()` | `60s` |
| `GET /config/policies` | politicas derivadas | `analytics.filters`, `analytics.sizing`, `analytics.exit_policy`, `analytics.strategy_runtime` | `60s` |
| `GET /logs/tail` | tail de logs | `logs/*.txt`, JSONL si se pide | `2s` |
| `GET /events/runtime` | runtime JSONL raw-normalized | `runtime_events.jsonl` | `2s` |
| `GET /events/research` | research JSONL raw-normalized | `candidate_outcomes.jsonl` | `2s` |
| `GET /control/state` | flags y ultimo estado control | `bot_runtime_state`, `control_commands` | `3s` |
| `GET /control/commands` | historial de comandos | `control_commands` | `3s` |
| `POST /control/commands` | crear comando | `control_commands` | n/a |

## Detailed endpoint contracts

### `GET /health`

Respuesta:

```json
{
  "data": {
    "service": "memebot3-api",
    "status": "ok",
    "version": "local",
    "time_utc": "2026-03-31T10:30:25.900000+00:00"
  },
  "meta": {
    "generated_at": "2026-03-31T10:30:25.900000+00:00",
    "degraded": false,
    "empty": false,
    "stale": false,
    "source_status": []
  }
}
```

### `GET /sources/status`

Debe inspeccionar como minimo:

- SQLite principal
- `runtime_events.jsonl`
- `candidate_outcomes.jsonl`
- `research_scorecard.json`
- `research_thresholds.json`
- parquet mas reciente
- `paper_portfolio.json`

Respuesta:

```json
{
  "data": {
    "sources": [
      {
        "source_key": "sqlite.main",
        "kind": "sqlite",
        "status": "ok",
        "path": "D:/Dev/Python/memebot3/data/memebotdatabase.db",
        "detail": "tokens=0 positions=0 revived_tokens=0"
      }
    ]
  },
  "meta": {}
}
```

### `GET /overview`

Construye la portada operativa. Debe funcionar aunque falte `bot_runtime_state`, pero en ese caso ira `degraded`.

`data` esperado:

```json
{
  "bot": {
    "bot_id": "main",
    "process_state": "running",
    "dry_run": true,
    "heartbeat_at": "2026-03-31T10:30:25.788966+00:00",
    "staleness": "fresh"
  },
  "runtime": {
    "discovery_paused": false,
    "buys_paused": false,
    "retrain_state": "idle",
    "reports_refresh_state": "idle"
  },
  "queue": {
    "pending": 0,
    "requeued": 0,
    "cooldown": 0,
    "oldest_first_seen_at": null
  },
  "wallet": {
    "wallet_sol": null,
    "wallet_checked_at": null
  },
  "positions": {
    "open_rows": 0,
    "closed_rows": 0,
    "win_rate_pct": null,
    "avg_pnl_pct": null
  },
  "ml": {
    "model_loaded": true,
    "activation_ready": false,
    "threshold": 0.0
  },
  "research": {
    "open_shadow_count": 0,
    "scorecard_generated_at": "2026-03-31T10:17:32.790668+00:00"
  }
}
```

### `GET /runtime/state`

Debe devolver un DTO normalizado desde `bot_runtime_state`.

```json
{
  "data": {
    "bot_id": "main",
    "updated_at": "2026-03-31T10:30:25.788966+00:00",
    "heartbeat_at": "2026-03-31T10:30:25.788966+00:00",
    "started_at": "2026-03-31T10:15:59.100000+00:00",
    "process_state": "running",
    "dry_run": true,
    "discovery_paused": false,
    "buys_paused": false,
    "wallet_sol": null,
    "wallet_checked_at": null,
    "queue_pending": 0,
    "queue_requeued": 0,
    "queue_cooldown": 0,
    "buy_limiter_in_window": 0,
    "buy_limiter_window_s": 3600,
    "retrain_state": "idle",
    "reports_refresh_state": "idle",
    "last_error": null,
    "stats": {},
    "ml_gate": {},
    "strategy_health": {},
    "research": {},
    "build_info": {}
  },
  "meta": {}
}
```

### `GET /runtime/events`

Query params:

- `limit`
- `before_ts`
- `event_type`
- `address`

Normalizacion minima por item:

```json
{
  "id": "2026-03-31T10:29:56.697731+00:00:requeue:9oSffR...",
  "ts_utc": "2026-03-31T10:29:56.697731+00:00",
  "event_type": "requeue",
  "address": "9oSffRHv2y3reTkCfgzuM93ugLqC5ZaTY57xyKEfpump",
  "summary": "requeue other after 3 attempts",
  "payload": {
    "reason": "other",
    "attempts": 3,
    "retries_left": 2,
    "backoff_s": 180,
    "first_seen_epoch_s": 1774952159.447187
  }
}
```

### `GET /runtime/strategy-health`

Si existe `bot_runtime_state.strategy_health_json`, manda ese contrato. Si no, fallback al ultimo `regime_health` de `runtime_events.jsonl`.

### `GET /discovery/feed`

Feed unificado del funnel discovery. Junta:

- `queue_add`, `requeue`, `queue_drop`, `buy`, `ml_decision`, `strategy_decision`
- `candidate_stage`, `candidate_decision`, `candidate_outcome`

Query params:

- `limit`
- `before_ts`
- `address`
- `stage`
- `decision_action`
- `reason`

Item normalizado:

```json
{
  "id": "2026-03-31T10:29:53.958292+00:00:candidate_decision:2m8ts5...",
  "stream": "research",
  "ts_utc": "2026-03-31T10:29:53.958292+00:00",
  "address": "2m8ts5Mhviqg9YgdngPxz2xwfhLRVgEMp6GBFERrpump",
  "symbol": "Elan",
  "regime": "dex_mature",
  "stage": "strategy",
  "action": "shadow",
  "reason": "strategy:confirm_ok",
  "severity": "info",
  "payload": {}
}
```

### `GET /discovery/summary`

Debe devolver agregados de ventana temporal, no solo conteos totales.

Query params:

- `window_min` default `60`

`data` minimo:

```json
{
  "window_min": 60,
  "queue": {
    "added": 0,
    "requeued": 0,
    "dropped": 0,
    "bought": 0
  },
  "candidate_decisions": [
    {
      "group": "rejected:no_liq",
      "count": 13
    }
  ],
  "candidate_stages": [
    {
      "group": "late_funnel",
      "count": 1
    }
  ],
  "requeue_reasons": []
}
```

### `GET /queue/summary`

Combina `bot_runtime_state` y breakdown reciente de `runtime_events.jsonl`.

`data` minimo:

```json
{
  "captured_at": "2026-03-31T10:30:25.788966+00:00",
  "pending": 0,
  "requeued": 0,
  "cooldown": 0,
  "oldest_first_seen_at": null,
  "recent_requeue_reasons": [
    {
      "reason": "other",
      "events": 1
    }
  ]
}
```

### `GET /queue/items`

Lee `bot_runtime_state.queue_items_json.items`.

Query params:

- `status`
- `limit`
- `address`

No debe recomputar la cola leyendo memoria ni reconstruirla desde JSONL.

### `GET /positions/open`

Devuelve posiciones con `closed = false`.

Item minimo:

```json
{
  "trade_id": 123,
  "address": "token-address",
  "symbol": "TOKEN",
  "opened_at": "2026-03-31T10:20:00+00:00",
  "qty": 1000,
  "buy_price_usd": 0.0001,
  "buy_amount_sol": 0.05,
  "entry_regime": "pump_early",
  "size_bucket": "standard",
  "size_multiplier": 0.6,
  "entry_ai_proba": 0.72,
  "entry_score_total": 66,
  "buy_liquidity_usd": 5000.0,
  "buy_market_cap_usd": 22000.0,
  "peak_price_usd": 0.00013,
  "highest_pnl_pct": 18.0
}
```

### `GET /trades/closed`

Devuelve posiciones con `closed = true`.

Query params:

- `limit`
- `before_ts`
- `outcome`
- `exit_reason`
- `entry_regime`

Cada fila debe incluir `total_pnl_pct` computado si la columna viene vacia.

### `GET /trades/{trade_id}`

Ficha completa del trade:

- row de `positions`
- metadata de `tokens`
- campos calculados de PnL
- contexto de regimen y execution metadata

### `GET /trades/{trade_id}/replay`

Contrato minimo:

```json
{
  "data": {
    "trade": {},
    "token": {},
    "entry_snapshot": {},
    "runtime_timeline": [],
    "research_timeline": [],
    "derived": {
      "first_seen_at": null,
      "minutes_first_seen_to_buy": null,
      "hold_minutes": null
    }
  },
  "meta": {}
}
```

Notas:

- `entry_snapshot` sale del parquet mas cercano por `address`
- `runtime_timeline` se filtra por `address` en `runtime_events.jsonl`
- `research_timeline` se filtra por `address` en `candidate_outcomes.jsonl`

### `GET /analytics/edge`

Debe serializar `analytics.reporting.summarize_edge()` tal cual, sin inventar metrica nueva.

### `GET /analytics/baseline`

Debe serializar `analytics.reporting.build_baseline_snapshot()`.

### `GET /ml/status`

Combina:

- `analytics.ai_predict.model_runtime_status()`
- `dataset_quality.json` si existe
- `train_status.json` si existe
- `recommended_threshold.json` si existe
- `bot_runtime_state.ml_gate_json` si existe

Contrato minimo:

```json
{
  "data": {
    "runtime": {
      "model_exists": true,
      "meta_exists": true,
      "model_loaded": true,
      "features_count": 42,
      "activation_ready": false,
      "dataset_quality_passed": null
    },
    "gate": {
      "mode": "shadow",
      "enforced": false,
      "threshold": 0.0
    },
    "train_status": null,
    "recommended_threshold": null,
    "dataset_quality": null
  },
  "meta": {}
}
```

`gate.mode` debe reflejar el modo real publicado por el bot en `bot_runtime_state.ml_gate_json` y hoy usa `legacy`, `shadow`, `enforce` u `off`.

### `GET /ml/research`

Debe devolver scorecard y thresholds, y marcar `stale` si los JSON van por detras del JSONL.

### `GET /config/effective`

Devuelve `analytics.reporting.snapshot_effective_config()`.

### `GET /config/policies`

Contrato:

```json
{
  "data": {
    "filters": {},
    "sizing": {},
    "exit": {},
    "strategy": {}
  },
  "meta": {}
}
```

Origens:

- `analytics.filters.describe_filter_policy()`
- `analytics.sizing.describe_sizing_policy()`
- `analytics.exit_policy.describe_exit_policy()`
- `analytics.strategy_runtime.describe_strategy_policy()`

### `GET /logs/tail`

Query params:

- `target`: `app`, `runtime_events`, `research_events`, o nombre whitelisted bajo `logs/`
- `lines`: default `200`, max `1000`

Restricciones:

- no paths arbitrarios
- no acceso fuera de `logs/` y `data/metrics/`

### `GET /events/runtime`

Vista raw-normalized de `runtime_events.jsonl`. Pensada para la pagina Logs and Events.

### `GET /events/research`

Vista raw-normalized de `candidate_outcomes.jsonl`.

### `GET /control/state`

Combina:

- flags de `bot_runtime_state`
- ultimo heartbeat
- ultimo comando ejecutado
- conteos por estado en `control_commands`

### `GET /control/commands`

Query params:

- `limit`
- `before_ts`
- `status`
- `command_type`

Respuesta por fila:

```json
{
  "id": 17,
  "bot_id": "main",
  "command_type": "pause_buys",
  "status": "done",
  "requested_by": "local-operator",
  "requested_at": "2026-03-31T10:31:00+00:00",
  "started_at": "2026-03-31T10:31:01+00:00",
  "finished_at": "2026-03-31T10:31:01.300000+00:00",
  "payload": {},
  "result": {
    "buys_paused": true
  },
  "error_text": null
}
```

### `POST /control/commands`

Request:

```json
{
  "bot_id": "main",
  "command_type": "refresh_reports",
  "payload": {
    "force": true,
    "include": ["baseline", "edge", "research"]
  },
  "requested_by": "local-operator",
  "idempotency_key": "ui-refresh-20260331-1032"
}
```

Respuesta `202`:

```json
{
  "data": {
    "id": 18,
    "status": "pending"
  },
  "meta": {
    "generated_at": "2026-03-31T10:32:00+00:00",
    "degraded": false,
    "empty": false,
    "stale": false,
    "source_status": [
      {
        "source_key": "sqlite.control_commands",
        "kind": "sqlite",
        "status": "ok",
        "updated_at": null,
        "detail": "inserted"
      }
    ]
  }
}
```

Validaciones:

- `requested_by` obligatorio hasta que exista auth real
- `bot_id` debe existir o resolver a `"main"`
- payload debe ajustarse al `command_type`
- duplicados por `idempotency_key` devuelven el comando ya creado

## Seguridad minima para v1

- los `GET` son read-only
- los `POST /control/commands` requieren cabecera `X-Operator-Id` o body `requested_by`
- solo se aceptan command types whitelisted
- la API no expone secretos ni contenido de `.env`
- `logs/tail` y eventos raw usan rutas y targets whitelisted

## Degradacion esperada por endpoint

- `overview`: puede seguir funcionando con DB + metrics aunque falte `bot_runtime_state`
- `runtime/state`: sin `bot_runtime_state` debe responder `503` o `200 degraded`, pero nunca inventar datos
- `queue/items`: sin `queue_items_json` debe responder `200` con `empty=true` y `degraded=true`
- `ml/status`: si faltan JSON opcionales, seguir con lo que haya
- `ml/research`: si scorecard esta stale respecto a JSONL, responder `200` con `stale=true`

## Consecuencia para el siguiente PR

`PR-UI-2` debe implementar primero:

1. `GET /health`
2. `GET /sources/status`
3. `GET /analytics/baseline`
4. `GET /analytics/edge`
5. `GET /config/effective`
6. `GET /config/policies`
7. `GET /ml/status`
8. `GET /ml/research`
9. `GET /events/runtime`
10. `GET /events/research`

`overview`, `runtime`, `queue` y `control` quedaran read-only parciales o degradados hasta `PR-UI-3` y `PR-UI-13`.
