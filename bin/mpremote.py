"""Shim that runs the venv's mpremote with whatever the launcher set up.

Use from outside Claude Code:
    python ${CLAUDE_PLUGIN_ROOT}/bin/mpremote.py ls :

Reuses bin/launch.py's bootstrap so the venv (and mpremote inside it) exists
before invocation. Forwards argv and exit code unchanged.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launch  # noqa: E402  bin/launch.py — same directory


def main() -> None:
    root, data = launch.resolve_env_dirs()
    data.mkdir(parents=True, exist_ok=True)
    venv_dir = data / "venv"
    launch.ensure_venv(venv_dir)
    launch.install_if_stale(root, data, venv_dir)

    if os.name == "nt":
        mpremote = venv_dir / "Scripts" / "mpremote.exe"
    else:
        mpremote = venv_dir / "bin" / "mpremote"

    if not mpremote.exists():
        launch.die(f"mpremote not found at {mpremote} after install")

    argv = [str(mpremote), *sys.argv[1:]]
    if os.name == "nt":
        sys.exit(subprocess.run(argv).returncode)
    os.execv(str(mpremote), argv)


if __name__ == "__main__":
    main()
