# UI Operations

Runbook operativo local para usar API, UI y bot sin acoplar la API al runtime live.

## Requisitos

- usar siempre `.\.venv\Scripts\python.exe`
- Node.js y `npm` disponibles para la UI
- la API y la UI estan pensadas para `127.0.0.1`

## Arranque rapido del stack

PowerShell:

```powershell
.\scripts\start_stack.ps1
```

Eso abre ventanas separadas para:

- API en `http://127.0.0.1:8000`
- UI en `http://127.0.0.1:5173`
- bot parado por defecto

Si quieres incluir tambien el bot desde PowerShell:

```powershell
.\scripts\start_stack.ps1 -IncludeBot
```

Si lo quieres en modo real:

```powershell
.\scripts\start_stack.ps1 -IncludeBot -BotRealMode
```

## Arranque por componente

API:

```powershell
.\scripts\start_api.ps1
```

Bot:

```powershell
.\scripts\start_bot.ps1
```

Bot en real mode:

```powershell
.\scripts\start_bot.ps1 -RealMode
```

UI:

```powershell
.\scripts\start_ui.ps1
```

Si falta `node_modules`:

```powershell
.\scripts\start_ui.ps1 -InstallIfMissing
```

## Control desde la UI

`Control Center` ahora puede arrancar y parar el bot, pero solo para el proceso gestionado por la propia UI.

- `admin` puede arrancar y parar el proceso del bot desde la UI
- si el bot se lanza manualmente por consola, la UI lo muestra como `external`
- un bot `external` no se para desde la UI; se para desde su consola original
- el arranque desde la UI soporta `dry-run` o `real`, y `file log` on/off

El flujo manual sigue igual y no se rompe:

```powershell
.\.venv\Scripts\python.exe -m run_bot --dry-run --log
```

## Login local

Modo por defecto:

- `UI_AUTH_MODE=local`

Credenciales locales por defecto:

- `viewer / viewer`
- `operator / operator`
- `admin / admin`

Override de usuarios:

```env
UI_LOCAL_USERS=viewer:mi-clave:viewer:Viewer;operator:otra-clave:operator:Operator;admin:clave-admin:admin:Admin
```

Modo dev solo loopback:

```env
UI_AUTH_MODE=dev
```

Ese modo evita login, pero solo debe usarse en `127.0.0.1` o `localhost`.

## Quality gate

Gate completo:

```powershell
.\scripts\quality_gate.ps1
```

Ejecuta:

- `scripts/runtime_smoke.py`
- `scripts/runtime_state_smoke.py`
- `scripts/bot_process_manager_smoke.py`
- `scripts/control_command_smoke.py`
- `scripts/api_smoke.py`
- `cd ui && npm run build`

Si solo quieres saltarte el smoke de command bus:

```powershell
.\scripts\quality_gate.ps1 --skip-control-smoke
```

## Backup basico

Backup estandar:

```powershell
.\scripts\backup_runtime.ps1
```

Incluyendo `.env`:

```powershell
.\scripts\backup_runtime.ps1 --with-env
```

Incluyendo tambien los ultimos logs:

```powershell
.\scripts\backup_runtime.ps1 --with-env --with-logs
```

El backup genera un zip en `backups/` con:

- `data/memebotdatabase.db` y sidecars WAL/SHM si existen
- `data/metrics/*.json` y feeds `jsonl`
- `data/features/*.parquet` y `*.csv`
- portfolios de paper y research
- `.env` solo si se pide explicitamente

## Restore basico

Restore con confirmacion explicita:

```powershell
.\scripts\restore_runtime.ps1 .\backups\memebot3-backup-YYYYMMDD-HHMMSS.zip --force
```

Si el archivo contiene `.env` y quieres restaurarlo:

```powershell
.\scripts\restore_runtime.ps1 .\backups\memebot3-backup-YYYYMMDD-HHMMSS.zip --force --with-env
```

Comportamiento:

- crea un pre-backup automatico antes del restore
- solo restaura rutas seguras bajo `data/`, `logs/` y `.env` si se autoriza
- no intenta reinyectar `memebotdatabase.db-wal` ni `memebotdatabase.db-shm`
- sobrescribe archivos incluidos en el zip
- no borra archivos actuales que no esten en el backup

## Notas operativas

- la API no importa `run_bot.py`
- la UI sigue funcionando aunque el bot este caido
- `start_stack.ps1` deja el bot parado por defecto
- el command bus sigue siendo para runtime commands; el start/stop del proceso vive aparte porque con el bot parado no hay consumidor
- `viewer` no puede lanzar `POST /api/v1/control/commands`
- `operator` y `admin` si pueden operar segun permisos
- `saved views` quedan persistidas en `sqlite.ui_saved_views`
