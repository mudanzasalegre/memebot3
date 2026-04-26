from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.config import CFG, PROJECT_ROOT
from ml.retrain import retrain_if_better
from ml.training_lock import acquire_lock, release_lock

STATUS_PATH = PROJECT_ROOT / "data" / "metrics" / "train_status.json"


def _write_status(extra: dict[str, Any]) -> None:
    payload = {"daemon_updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **extra}
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def train_once() -> bool:
    ttl = int(getattr(CFG, "ML_TRAINING_LOCK_TTL_S", 1800) or 1800)
    if not acquire_lock(ttl_s=ttl):
        _write_status({"status": "locked"})
        return False
    try:
        updated = retrain_if_better()
        _write_status({"status": "trained" if updated else "not_promoted", "updated": bool(updated)})
        return bool(updated)
    except Exception as exc:
        _write_status({"status": "failed", "error": str(exc)})
        raise
    finally:
        release_lock()


def run_daemon(*, interval_s: int = 900) -> None:
    while True:
        train_once()
        time.sleep(int(interval_s))


if __name__ == "__main__":
    run_daemon()
