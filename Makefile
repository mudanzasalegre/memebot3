test:
	pytest -q

ml-audit:
	python tools/audit_ml_baseline.py

ml-status:
	python tools/ml_status.py

backtest:
	python backtest/replay.py --policy lane_aware

train:
	python scripts/train_once.py

smoke:
	bash scripts/smoke_ml.sh
