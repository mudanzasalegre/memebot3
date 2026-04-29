from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.training_daemon import train_once


if __name__ == "__main__":
    raise SystemExit(0 if train_once() else 1)
