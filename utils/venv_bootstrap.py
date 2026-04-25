from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


_BOOTSTRAP_ENV_FLAG = "MEMEBOT_VENV_BOOTSTRAPPED"


def ensure_project_venv(anchor_file: str, *, module_name: str | None = None) -> None:
    """
    Re-lanza el proceso con ``.venv\\Scripts\\python.exe`` si existe y el
    intérprete actual no pertenece al entorno virtual del proyecto.

    Está pensado para módulos CLI como ``ml.train`` / ``ml.retrain`` que
    pueden invocarse accidentalmente con el Python global del sistema.
    """
    if os.environ.get(_BOOTSTRAP_ENV_FLAG) == "1":
        return

    project_root = Path(anchor_file).resolve().parents[1]
    venv_python = (project_root / ".venv" / "Scripts" / "python.exe").resolve()
    if not venv_python.exists():
        return

    try:
        current_python = Path(sys.executable).resolve()
    except Exception:
        current_python = Path(sys.executable)

    if current_python == venv_python:
        return

    if module_name and module_name != "__main__":
        argv = [str(venv_python), "-m", str(module_name), *sys.argv[1:]]
    else:
        argv = [str(venv_python), *sys.argv]

    env = os.environ.copy()
    env[_BOOTSTRAP_ENV_FLAG] = "1"
    completed = subprocess.run(argv, env=env, check=False)
    raise SystemExit(int(completed.returncode))


__all__ = ["ensure_project_venv"]
