# Preflight

Use the project virtual environment, not the global Anaconda Python:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tools\preflight.py --run-tests
```

The preflight writes `data/metrics/preflight_status.json`.

Checks covered:

- imports `config.config.CFG`;
- compiles critical runtime, analytics, ML and policy modules;
- verifies `.env.example` and `config/profiles/` exist;
- optionally runs the full pytest suite.

This milestone does not change trading behavior.
