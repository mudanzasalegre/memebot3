# AutoResearch Runbook

AutoResearch is a paper/replay research loop. It must never enable live trading,
copy secrets, edit `.env`, or change buyer/seller/wallet runtime code.

## Smoke

Run the local end-to-end smoke:

```powershell
.\.venv\Scripts\python.exe tools\autoresearch_smoke.py
```

The smoke performs these checks:

- Generate 3 candidate policies.
- Validate schema and safety.
- Create isolated candidate sandboxes.
- Run local replay from local report files.
- Evaluate objective score.
- Update `data/research_runs/scoreboard.json` and `scoreboard.md`.
- Build `data/research_runs/api_budget.json`.
- Export a safe paper candidate profile under `config/profiles/`.
- Verify generated artifacts keep live flags false and contain no secrets.

The smoke fills missing local replay reports with minimal fixture reports. It
does not overwrite existing metric reports unless explicitly called with:

```powershell
.\.venv\Scripts\python.exe tools\autoresearch_smoke.py --overwrite-fixture-metrics
```

## Final Gate

Run the final strategy gate:

```powershell
.\.venv\Scripts\python.exe scripts\strategy_quality_gate.py --warn-only
```

The gate validates:

- `AUTORESEARCH_LIVE_PROMOTION_ENABLED=false`
- `AUTORESEARCH_AUTO_LIVE_PROMOTE=false`
- `AUTORESEARCH_LLM_CAN_TOUCH_LIVE=false`
- Candidate policies have `live_allowed=false`.
- Candidate policies do not touch forbidden env/API/secrets keys.
- `research_loop/safety.yaml` exists.
- `strategy_proposals/schema.autoresearch.json` exists.
- `research_loop/objectives.yaml` and `research_loop/objectives.py` exist.
- `data/research_runs/api_budget.json` exists and has no rejecting comparison.
- `data/research_runs/scoreboard.json` exists.
- At least one `paper_research_candidate_*.env` profile exists and remains paper-only.

## Full Local Validation

Use this sequence after AutoResearch changes:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tools\regenerate_core_reports.py
.\.venv\Scripts\python.exe tools\autoresearch_smoke.py
.\.venv\Scripts\python.exe scripts\strategy_quality_gate.py --warn-only
```

## One-Command Startup

The normal operator command is now:

```powershell
.\scripts\start_stack.ps1 -IncludeBot
```

That command runs the startup preflight first:

- `pytest -q`
- core report regeneration
- isolated AutoResearch smoke in a temporary root
- strategy quality gate in `--warn-only` mode

Then it launches the bot in dry-run paper mode and starts AutoResearch as a
paper/replay daemon. Use `-SkipAutoResearch` only when you intentionally want
bot-only startup, and `-SkipStartupPreflight` only for a fast restart after a
known-good validation.

For a foreground one-cycle AutoResearch validation without leaving bot/API/UI
processes open:

```powershell
.\tests\test_start_stack_autoresearch.ps1
```

## Hard Boundaries

Do not use AutoResearch to:

- Set `DRY_RUN=0`.
- Enable any live canary flag.
- Enable live auto-promotion.
- Touch secrets, wallet, signer, buyer or seller code.
- Increase API rate limits or provider polling cadence.
- Use LLM output as a direct trading action.

Paper candidate profiles are review artifacts only. Live activation remains a
manual, separate operator decision.
