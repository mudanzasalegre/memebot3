from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.ai_predict import should_buy
from features.builder import build_feature_vector


def main() -> int:
    parser = argparse.ArgumentParser(description="Explain local ML features/proba for a token JSON.")
    parser.add_argument("--mint")
    parser.add_argument("--json", help="Inline token JSON")
    parser.add_argument("--file", help="Token JSON file")
    args = parser.parse_args()
    if args.file:
        token = json.loads(Path(args.file).read_text(encoding="utf-8"))
    elif args.json:
        token = json.loads(args.json)
    else:
        token = {"address": args.mint}
    vec = build_feature_vector(token)
    proba = should_buy(vec)
    print(json.dumps({"proba": proba, "features": vec.to_dict()}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
