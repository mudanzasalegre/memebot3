from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.provider_health import provider_health_snapshot


def main() -> None:
    print(json.dumps(provider_health_snapshot(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
