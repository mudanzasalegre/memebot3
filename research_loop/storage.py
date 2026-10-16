"""Publish complete management documents; not a multi-file transaction.

Keep the existing report JSON serialization (including default=str). A unique
sibling temporary file is flushed and fsync'ed before replacement. Original
documents survive failures before replace. This is not a hardware power-loss
guarantee, an append-journal protocol or an acceptance of financial evidence.
"""
import json
import os
from pathlib import Path
import tempfile
from typing import Any


def write_text_document(path: Path, text: str) -> None:
    path=Path(path).resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temporary=tempfile.mkstemp(dir=path.parent,prefix=f'.{path.name}.',suffix='.tmp')
    try:
        try:
            handle=os.fdopen(descriptor,'w',encoding='utf-8')
        except BaseException:
            try:os.close(descriptor)
            except OSError:pass
            raise
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json_document(path: Path, payload: Any) -> None:
    write_text_document(path,json.dumps(payload,indent=2,sort_keys=True,default=str))
