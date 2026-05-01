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
- parses `.env.example`;
- parses every `config/profiles/*.env` file;
- verifies `data/metrics` and `docs` exist;
- runs report builders against an empty temporary data root;
- optionally runs the full pytest suite.

This milestone does not train models, change `.env`, alter strategy, buy, or simulate trades.
