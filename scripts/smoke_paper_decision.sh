#!/usr/bin/env bash
set -euo pipefail
python - <<'PY'
from analytics.ml_policy import decide_ml_action

d = decide_ml_action(
    token={"address": "smoke", "entry_lane": "pump_early_pumpswap_profit"},
    feature_row={},
    proba=0.01,
    base_rules_passed=True,
    dry_run=True,
    live=False,
)
print(d)
assert d.allow_buy
PY
