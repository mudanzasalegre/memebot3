# Preflight

Use the project virtual environment, not the global Anaconda Python:

```powershell
.\.venv\Scripts\python.exe -m tools.preflight --run-tests
```

The preflight writes `data/metrics/preflight_status.json`.

The full-suite subprocess has a default limit of 3,600 seconds. Use
`--pytest-timeout-seconds N` to set an integer budget from 1 to 86,400 seconds;
the limit applies to pytest, not the entire preflight command. Invalid budgets
are rejected before report directories or status files are written.

Timeouts are recorded with `returncode: 124`, `timed_out: true` and bounded
output tails. OS launch errors use `returncode: 127` and retain only the error
type. These checks fail the preflight: the native command writes a failed status
and exits `1`; it does not attest that an interrupted test suite passed.
Python's subprocess timeout handling kills and waits for the direct child;
this is not a process-tree cleanup guarantee.

If pytest was already run to successful completion against the same sources,
`--external-pytest-passed` records that caller attestation without running it
again. Never use that flag after a skipped, failed or timed-out suite.
`scripts/start_stack.ps1` already runs pytest directly before its attestation;
the repaired timeout affected the standalone `--run-tests` path.

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
