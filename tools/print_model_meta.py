from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.config import CFG


def main() -> int:
    path = CFG.MODEL_PATH.with_suffix(".meta.json")
    if not path.exists():
        print(f"missing_model_meta={path}")
        return 1
    print(json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
