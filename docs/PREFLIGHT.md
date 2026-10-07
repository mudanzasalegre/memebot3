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
- validates `config/profiles/paper_hotfix_0707.env` for PR-00 safety invariants;
- rejects PR-00 live flags and paper micro caps set to `0` because `0` means unlimited at runtime;
- validates live-canary templates against `docs/LIVE_PROMOTION_PROTOCOL.md`;
- rejects live profiles without `LIVE_CANARY_PROTOCOL=manual_canary_v1` or finite canary caps;
- redacts secret-like env keys before writing profile values into the status payload;
- verifies `data/metrics` and `docs` exist;
- runs report builders against an empty temporary data root;
- optionally runs the full pytest suite.

This milestone does not train models, change `.env`, alter strategy, buy, or simulate trades.
