# MemeBot 3 UI

Shell operacional React + Vite para `PR-UI-15`.

## Arranque local

1. API:

```powershell
.\.venv\Scripts\python.exe -m api
```

2. UI:

```powershell
cd ui
npm install
npm run dev
```

La UI usa `vite` en `http://127.0.0.1:5173` y proxy a `http://127.0.0.1:8000` para `/api`.

Atajo operativo para levantar toda la plataforma:

```powershell
.\scripts\start_stack.ps1
```

Ese atajo ahora levanta `API + UI` y deja el bot parado por defecto. Si quieres abrir tambien una consola del bot desde PowerShell:

```powershell
.\scripts\start_stack.ps1 -IncludeBot
```

Desde la propia UI, en `Control Center`, un usuario con permiso `admin` puede arrancar y parar el bot gestionado por la interfaz.

## Auth local

Por defecto la API arranca en `UI_AUTH_MODE=local` con estas cuentas locales:

- `viewer / viewer`
- `operator / operator`
- `admin / admin`

Puedes sobrescribirlas en `.env` con:

```env
UI_LOCAL_USERS=viewer:mi-clave:viewer:Viewer;operator:otra-clave:operator:Operator;admin:clave-admin:admin:Admin
```

Modo de emergencia solo loopback:

```env
UI_AUTH_MODE=dev
```

Ese modo evita login pero solo debe usarse en `127.0.0.1` o `localhost`.

## Build

```powershell
cd ui
npm run build
```

Quality gate completo del stack:

```powershell
.\scripts\quality_gate.ps1
```

## Variables opcionales

- `VITE_API_BASE_URL`
- `VITE_API_PROXY_TARGET`
- `UI_AUTH_MODE`
- `UI_LOCAL_USERS`
- `UI_SESSION_COOKIE_NAME`
- `UI_SESSION_TTL_SECONDS`

## Runbook

Operación diaria, backup y restore:

- [docs/UI_OPERATIONS.md](../docs/UI_OPERATIONS.md)
