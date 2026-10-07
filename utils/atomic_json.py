"""Replace one JSON document only after its complete contents are fsync'ed.

This protects against partial process writes, not every filesystem/hardware
power-loss scenario. No permissive JSON constants or implicit object coercion.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, payload: Any) -> None:
    path = Path(path)
    text = json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        # Only our individually-created temporary file, never a recursive path.
        Path(temporary).unlink(missing_ok=True)


def read_json_strict(path: Path) -> Any:
    def invalid(value: str):
        raise ValueError(f"Nonfinite JSON constant: {value}")
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=invalid)
