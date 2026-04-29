#!/usr/bin/env bash
set -euo pipefail
python tools/ml_status.py --no-fail-if-missing-model
python -m ml.segment_report --no-fail-if-missing
python backtest/replay.py --policy rules_only --no-fail-if-no-data
