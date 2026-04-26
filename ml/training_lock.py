from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from config.config import PROJECT_ROOT

LOCK_PATH = PROJECT_ROOT / "data" / "metrics" / "training.lock"


def acquire_lock(path: Path = LOCK_PATH, *, ttl_s: int = 1800) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if now - float(payload.get("created_epoch_s") or 0.0) < int(ttl_s):
                return False
        except Exception:
            pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "created_epoch_s": now}), encoding="utf-8")
    os.replace(tmp, path)
    return True


def release_lock(path: Path = LOCK_PATH) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


__all__ = ["LOCK_PATH", "acquire_lock", "release_lock"]
