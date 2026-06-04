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
| `JUP_API_KEY` | Jupiter si tu plan lo requiere. |
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

Ese comando tambien inicia AutoResearch en una ventana propia, en modo daemon
paper/replay seguro. Por defecto arranca con `AutoResearchMaxCandidates=3`,
`MaxParallel=1`, intervalo de 6 horas, live promotion apagado y LLM live-touch
apagado.

Si quieres levantar API + UI + bot sin AutoResearch:

```powershell
.\scripts\start_stack.ps1 -IncludeBot -SkipAutoResearch
```

Opciones utiles:

```powershell
.\scripts\start_stack.ps1 -IncludeBot -AutoResearchSpace moonshot_micro -AutoResearchMaxCandidates 10
.\scripts\start_stack.ps1 -IncludeBot -AutoResearchOnce -AutoResearchNoPaperPromote
.\scripts\start_stack.ps1 -IncludeBot -AutoResearchRegenerateReports
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
