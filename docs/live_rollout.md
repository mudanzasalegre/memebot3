# Live Rollout

Do not enable live `enforce` globally. Promote by lane only after reports show the model improves realized PnL and keeps jackpot capture acceptable.

1. Observation: `ML_GATE_MODE=shadow`, `ML_REJECT_SHADOW_ENABLED=true`.
2. Paper lane-aware: `DRY_RUN=true`, `ML_GATE_MODE=lane_aware`, research may enforce in paper.
3. Live conservative: `DRY_RUN=false`, live profit `sizing_only`, research `shadow`.
4. Risk veto live: enable only after the severe-loss model reduces losses without missing jackpots.
5. Enforce by lane: manual confirmation after `lane_promotion_status.json` recommends `enforce`.

Check readiness without changing config:

```bash
python tools/check_rollout_readiness.py --phase 2
```
