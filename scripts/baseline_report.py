from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.reporting import build_baseline_snapshot, render_baseline_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera baseline de config, DB y parquet.")
    parser.add_argument(
        "--write-docs",
        default="docs/BASELINE.md",
        help="Ruta del markdown a escribir. Usa cadena vacia para no escribir.",
    )
    args = parser.parse_args()

    snapshot = build_baseline_snapshot()
    markdown = render_baseline_markdown(snapshot)
    print(markdown)

    target = str(args.write_docs or "").strip()
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
