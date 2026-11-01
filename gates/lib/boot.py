"""Trusted bootstrap for the gates' lane-venv Python (G70-FIX r2). Stdlib only; bound read-only.

    <lane python> -I -S -B <gates/lib>/boot.py pytest <pytest args...>  gate 70's sandboxed pytest
    <lane python> -I -S -B <gates/lib>/boot.py identity  sandbox_identity's binding half

Why: a normal interpreter start runs `site`, which processes the lane venv's .pth files (the
sandbox rewrites the editable one to name <snap>/src) and imports `sitecustomize` /
`usercustomize` from sys.path. A candidate-tracked src/sitecustomize.py therefore runs BEFORE any
trusted code, under `-P` and `-I` alike. `-S` skips `site` entirely: no .pth, no startup hook.
This file then builds sys.path by hand, in this order: the stdlib (already there), this trusted
directory (so `-p gates_nodes` resolves to the bound trusted copy), the venv's own site-packages
(runner-owned, read-only), and the snapshot's src/ LAST, so a candidate module can neither shadow
the trusted reporter nor a third-party package, and `chronos` can only come from the snapshot (a
copy in site-packages would win, and the identity mode then refuses it).
Under -S `sys.prefix` is the base Python, so the venv is found from `sys.executable`.
"""

import glob
import os
import sys


def main() -> int:
    mode, args = sys.argv[1], sys.argv[2:]
    here = os.path.dirname(os.path.abspath(__file__))
    venv = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    sites = []
    if os.path.isfile(os.path.join(venv, "pyvenv.cfg")):
        sites = sorted(glob.glob(os.path.join(venv, "lib*", "python*", "site-packages")))
    src = os.path.join(os.getcwd(), "src")
    sys.path[:] = [*sys.path, here, *sites, src]
    if mode == "pytest":
        import pytest

        return int(pytest.main(args))
    if mode == "identity":
        import chronos

        got, want = os.path.realpath(chronos.__file__), os.path.realpath(src)
        return 0 if got.startswith(want + os.sep) else 3
    print(f"boot.py: unknown mode {mode!r}", file=sys.stderr)
    return 64


sys.exit(main())
