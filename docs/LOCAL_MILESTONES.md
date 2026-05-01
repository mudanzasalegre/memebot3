# Local Milestones

This workspace has no `.git`, so PR-00..PR-31 are tracked as local milestones.

| Milestone | Status | Notes |
|---|---|---|
| PR--01 Preflight | implemented | Uses `.venv`, fixes current `CFG` AST-test failure, adds preflight tooling. |
| PR-00 Baseline snapshot | implemented | `analytics/baseline_snapshot.py`, `tools/current_baseline_snapshot.py`. |
| PR-01 Funnel Attribution v2 | implemented | Adds final state metadata, shadow/outcome flags and later peak. |
| PR-02 Decision Ledger | implemented | `features/decision_store.py`, `analytics/decision_ledger.py`, best-effort runtime append. |
| PR-03 Dataset Hygiene v2 | implemented | Expanded sample types and productive-training predicates. |
| PR-04 Label Builder | implemented | `ml/label_builder.py`. |
| PR-05 Feature Set v2 | implemented | `ml/feature_sets.py`. |
| PR-06 Risk Model | implemented | `ml/train_risk_model.py`, runtime wrapper. |
| PR-07 EV Model | implemented | `ml/train_ev_model.py`, runtime wrapper. |
| PR-08 Runner Model | implemented | `ml/train_runner_model.py`, runtime wrapper. |
| PR-09 Continuation Model | implemented | `ml/train_continuation_model.py`, runtime wrapper. |
| PR-10 Exit Model | implemented | `ml/train_exit_model.py`, runtime wrapper. |
| PR-11 TradeDecision v2 | implemented | `execution/trade_decision.py`, `runtime/entry_policy.py`. |
| PR-12 Policy Score | implemented | `runtime/policy_score.py`. |
| PR-13 Policy Modes | implemented | `runtime/policy_modes.py`; live enforce is blocked by default. |
| PR-14 Dynamic Thresholds | implemented | `runtime/dynamic_thresholds.py`. |
| PR-15 Policy Replay | implemented | Existing replay extended with model/combined policies. |
| PR-16 Green Learned Policy | implemented | Safe helper in `runtime/learned_policies.py`. |
| PR-17 Research Rank Learned Canary | implemented | Safe helper in `runtime/learned_policies.py`. |
| PR-18 Late Momentum Continuation | implemented | Safe helper in `runtime/learned_policies.py`. |
| PR-19 Bird Runner Exit | implemented | `analytics/bird_runner_exit.py`, explicit profile support. |
| PR-20 Exit Policy Selector | implemented | `analytics/exit_policy_selector.py`. |
| PR-21 Runner Capture Optimizer | implemented | `analytics/runner_capture_optimizer.py`. |
| PR-22 Walk-forward Training | implemented | `ml/walk_forward.py`, `tools/walk_forward_train.py`. |
| PR-23 Model Registry Families | implemented | `ml/model_registry.py` supports family paths. |
| PR-24 Drift Monitor | existing/compatible | Existing defensive `analytics/drift_monitor.py`. |
| PR-25 Dynamic Policy Tuner | implemented | `runtime/policy_tuner.py`. |
| PR-26 Candidate Policy Profiles | implemented | `strategy_proposals/` schema and candidate dirs. |
| PR-27 Paper Forward Evaluator | implemented | `analytics/paper_forward.py`. |
| PR-28 Live Canary Controller v2 | implemented | `runtime/live_canary_v2.py`. |
| PR-29 Strategy Quality Gate v2 | implemented | Existing script extended. |
| PR-30 StrategyProposal Schema | implemented | `strategy_proposals/schema.json`, validator. |
| PR-31 Proposal Review CLI | implemented | `tools/review_strategy_proposal.py`. |

Validation command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```
