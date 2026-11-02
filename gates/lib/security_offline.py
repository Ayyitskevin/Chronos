"""Gate 40's offline security driver (TRUSTED; bound read-only into the sandbox).

Run as `<tools venv python> -I -B security_offline.py` from the snapshot root. It runs the scans of
the release security gate (the TRUSTED base's scripts/verify_release_security.py, overlaid on the
snapshot) minus pip-audit, which a trusted step already ran. The interpreter is the NON-EDITABLE
tools venv with -I (no cwd, no PYTHONPATH), so no candidate byte is on sys.path. Every scanner
child gets -I -B too, because interpreter flags are not inherited by a child: `python -m bandit`
and `python -m detect_secrets...` cannot resolve a candidate bandit.py / detect_secrets/ at the
snapshot root or under src/.
"""

import importlib.util
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

root, copy = Path.cwd(), Path(os.environ["GATE_IO"]) / ".secrets.baseline"
spec = importlib.util.spec_from_file_location(
    "release_security", root / "scripts/verify_release_security.py"
)
gate = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
gate._require_exact_tool_versions(metadata.version)
tracked = tuple(p for p in gate._tracked_files(root) if p != ".secrets.baseline")
for command in gate.build_scan_commands(
    python=Path(sys.executable), baseline=copy, tracked_files=tracked
):
    if command.name != "runtime dependency audit":
        argv = tuple(command.argv)
        if argv[0] == sys.executable and argv[1:2] == ("-m",):
            argv = (argv[0], "-I", "-B", *argv[1:])
        subprocess.run(argv, cwd=root, check=True)
gate.verify_git_history_secrets(root, copy)
