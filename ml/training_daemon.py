from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from config.config import CFG, PROJECT_ROOT
from ml.train import TRAIN_STATUS_JSON, feature_dataset_snapshot
from ml.retrain import retrain_if_better
from ml.training_lock import acquire_lock, release_lock
from ml.runner_advisory_learning import train_runner_advisory

STATUS_PATH = TRAIN_STATUS_JSON
log = logging.getLogger(__name__)


def _read_status() -> dict[str, Any]:
    if not STATUS_PATH.exists():
        return {}
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_status(extra: dict[str, Any], *, preserve_training_status: bool = False) -> None:
    existing = _read_status() if preserve_training_status else {}
    status = extra.get("status")
    payload = {
        **existing,
        "daemon_updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feature_dataset": feature_dataset_snapshot(),
        **extra,
    }
    if preserve_training_status and existing and status is not None:
        payload["daemon_status"] = status
        payload["status"] = existing.get("status", status)
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_PATH.with_name(f".{STATUS_PATH.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(temporary, STATUS_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def train_once() -> bool:
    ttl = int(getattr(CFG, "ML_TRAINING_LOCK_TTL_S", 1800) or 1800)
    if not acquire_lock(ttl_s=ttl):
        _write_status({"status": "locked", "daemon_status": "locked", "updated": False})
        return False
    try:
        errors: dict[str, str] = {}
        updated = False
        try:
            updated = retrain_if_better()
        except Exception as exc:
            errors["entry_training"] = type(exc).__name__
            log.exception("Entry training failed; inspect the atomic primary selector for authoritative state")
        advisory = {"status": "missing_dataset", "updated": False}
        if feature_dataset_snapshot().get("usable"):
            try:
                advisory = train_runner_advisory()
            except Exception as exc:
                errors["runner_advisory"] = type(exc).__name__
                advisory = {"status": "failed", "updated": False, "error_type": type(exc).__name__}
                log.exception("Runner advisory training failed; manifest preserved")
        _write_status(
            {"status": "failed" if errors else "trained" if updated else "not_promoted",
             "updated": bool(updated), "runner_advisory": advisory, "training_errors": errors},
            preserve_training_status=True,
        )
        return bool(updated or advisory.get("updated"))
    except Exception as exc:
        _write_status({"status": "failed", "daemon_status": "failed", "error": str(exc)})
        raise
    finally:
        release_lock()


def run_daemon(*, interval_s: int = 900) -> None:
    while True:
        try:
            train_once()
        except Exception:
            log.exception("Training cycle failed; retrying without losing its previous artifacts")
        time.sleep(max(1, int(interval_s)))


if __name__ == "__main__":
    run_daemon()
