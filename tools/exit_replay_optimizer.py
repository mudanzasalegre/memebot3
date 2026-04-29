from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.exit_replay_optimizer import write_exit_replay


def main() -> None:
    print(json.dumps(write_exit_replay(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
