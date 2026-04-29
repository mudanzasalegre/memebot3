from __future__ import annotations

import argparse
import subprocess
import sys
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


def _require_project_python() -> None:
    print(f"python_executable={sys.executable}")
    if EXPECTED_PYTHON.exists() and Path(sys.executable).resolve() != EXPECTED_PYTHON.resolve():
        raise SystemExit(
            "Quality gate must be executed with the project venv. "
            f"Use: {EXPECTED_PYTHON} scripts/quality_gate.py"
        )


def _run(command: list[str], *, workdir: Path) -> None:
    print(f"[quality_gate] running: {' '.join(command)}")
    subprocess.run(command, cwd=workdir, check=True)


def _resolve_npm_command() -> list[str]:
    if sys.platform.startswith("win"):
        npm = shutil.which("npm.cmd") or shutil.which("npm")
    else:
        npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm is required for the UI build quality gate")
    return [npm]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the PR-UI-16 local quality gate.")
    parser.add_argument("--skip-control-smoke", action="store_true")
    parser.add_argument("--skip-ui-build", action="store_true")
    args = parser.parse_args()

    _require_project_python()

    python = str(Path(sys.executable).resolve())
    script_dir = PROJECT_ROOT / "scripts"
    _run([python, str(script_dir / "runtime_smoke.py")], workdir=PROJECT_ROOT)
    _run([python, str(script_dir / "runtime_state_smoke.py")], workdir=PROJECT_ROOT)
    _run([python, str(script_dir / "bot_process_manager_smoke.py")], workdir=PROJECT_ROOT)
    _run([python, str(script_dir / "sniper_quality_gate.py"), "--warn-only"], workdir=PROJECT_ROOT)
    _run([python, str(script_dir / "strategy_quality_gate.py"), "--warn-only"], workdir=PROJECT_ROOT)
    if not args.skip_control_smoke:
        _run([python, str(script_dir / "control_command_smoke.py")], workdir=PROJECT_ROOT)
    _run([python, str(script_dir / "api_smoke.py")], workdir=PROJECT_ROOT)
    if not args.skip_ui_build:
        _run(_resolve_npm_command() + ["run", "build"], workdir=PROJECT_ROOT / "ui")

    print("quality_gate=ok")


if __name__ == "__main__":
    main()
