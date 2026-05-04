# Local Milestones PR-00..PR-36

This workspace has no `.git`, so the PR plan is tracked as local milestones.

Validation command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Invariants

- No automatic live activation.
- No automatic model promotion.
- No LLM trading.
- Socials are never a hard gate.
- Rule-based fallback remains available.

## Milestones

| Milestone | Local status | Notes |
|---|---|---|
| PR-00 Preflight | implemented | Extended `.venv` preflight, profile/env checks, no-data report builders. |
| PR-01 Position limits semantics | implemented | `cap=-1` unlimited, `cap=0` blocked, `cap>0` max open. |
| PR-02 Green sniper age score | implemented | Shared token age helper and score normalization from `created_at`/`createdAt`. |
| PR-03 Missed pumps confirmed outcomes | implemented | Confirmed winner/loser requires outcome-confirmed sample or flag. |
| PR-04 Late momentum route policy | implemented | Paper route proxy tag; live no-route blocks/shadows. |
| PR-05 Config effect audit | implemented | `tools/config_effect_audit.py` reports active/placebo flags. |
| PR-06 Baseline snapshot | implemented | Adds severe losses, runner counts and mcap buckets. |
| PR-07 Funnel Attribution v2 | implemented | Final-state attribution hardened; `late_funnel` cannot be final blocker. |
| PR-08 Decision Ledger | implemented | Canonical action normalization and outcome linking fields. |
| PR-09 Dataset Hygiene v2 | implemented | Execution-blocked excluded from productive training; lane shadows are outcome samples. |
| PR-10 Trade Diagnostics | implemented | Entry subtype, buckets, severe and runner metrics. |
| PR-11 Green Sniper Risk Guard | implemented | Guard integrated behind existing flag. |
| PR-12 Liquidity Guard | implemented | Guard integrated in green sniper and late momentum. |
| PR-13 Early Dump Cut | implemented | Pure helper plus runtime monitor wrapper. |
| PR-14 Policy Replay Engine | implemented | Plan aliases added while preserving existing policies. |
| PR-15 Runner Capture Report | implemented | Adds gt_500, exit_profile and lane grouping. |
| PR-16 Label Builder | implemented | Existing label builder retained with anti-leakage tests. |
| PR-17 Feature Sets v2 | implemented | Existing explicit feature sets retained. |
| PR-18 Severe Loss Risk Model | implemented | Manual trainer/runtime retained; no auto-promotion. |
| PR-19 EV Model | implemented | Manual trainer/runtime retained. |
| PR-20 Runner Model | implemented | Manual trainer/runtime retained. |
| PR-21 Continuation Model | implemented | Manual trainer/runtime retained. |
| PR-22 TradeDecision v2 | implemented | Existing v2 retained. |
| PR-23 Policy Score | implemented | Existing multi-objective score retained. |
| PR-24 Dynamic Thresholds | implemented | Existing offline threshold manager retained. |
| PR-25 Learned Policy v1 | implemented | Safe paper/live mode helpers retained. |
| PR-26 Bird Runner Exit | implemented | Existing profile support retained. |
| PR-27 Exit Selector | implemented | Existing selector retained. |
| PR-28 Walk-forward Training | implemented | Existing manual pipeline retained. |
| PR-29 Model Registry Families | implemented | Existing family registry retained. |
| PR-30 Drift Monitor | implemented | Defensive monitor retained. |
| PR-31 Offline Tuner | implemented | Existing candidate generator retained. |
| PR-32 Candidate Policy Profiles | implemented | Existing schema and validator retained. |
| PR-33 Paper Forward Evaluator | implemented | Runtime/tool wrappers added. |
| PR-34 Live Canary Controller v2 | implemented | Paper-forward and risk-low gates added. |
| PR-35 Strategy Quality Gate v2 | implemented | Live/replay/paper/manual approval checks extended. |
| PR-36 StrategyProposal LLM-ready | implemented | Existing proposal README/schema/review CLI retained; no LLM integration. |

## UI Upgrade

| Milestone | Local status | Notes |
|---|---|---|
| Learned Policy UI control plane | implemented | Added `/policy` plus read-only API endpoints for safety gates, replay, decision ledger, funnel attribution, diagnostics, runner capture, model families and proposals. |
| Sniper UI confirmed outcomes | implemented | Missed-pumps table now shows classification and outcome confirmation so hot-seen rows are not confused with confirmed outcomes. |
| Config UI effect audit | implemented | Config Center now surfaces the config-effect audit summary alongside effective config and derived policies. |
