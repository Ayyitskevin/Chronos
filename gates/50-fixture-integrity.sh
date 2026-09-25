#!/usr/bin/env bash
# 50-fixture-integrity.sh — pinned fixtures verify against their bytes; no silent drift.
# chronos has no SHA256SUMS files: this is the `sha256sum -c` equivalent over every tracked
# tests/fixtures/**/manifest.json (its "files" map) and tests/fixtures/**/*.meta.json (trace_sha256 →
# the sibling <stem>.csv; pine_sha256 → the one tracked research/pine/<catalog_number>_*.pine).
# input_config_sha256 hashes an in-document object (the loader's check); other *_sha256 keys, non-null attestations fail.
# Every manifest and pinned file must be a TRACKED regular file whose real path stays under its root;
# it is opened O_NOFOLLOW|O_NONBLOCK and fstat-checked, so a link or FIFO is refused before it is read or hashed.
set -uo pipefail
g=fixture-integrity
top="$(git rev-parse --show-toplevel 2>/dev/null)" && cd "$top" || { echo "FAIL: $g — not inside a git work tree; run the gate from the repo root" >&2; exit 1; }
verify() { python3 - <<'PY'
import hashlib, json, os, re, stat, subprocess, sys
from pathlib import PurePosixPath as P
def die(msg, hint="fix the manifest, then re-run"):
    print(f"{msg}; {hint}"); sys.exit(1)
out = subprocess.run(["git", "ls-files", "-s", "-z", "--", "tests/fixtures", "research/pine"], capture_output=True, check=True).stdout
modes = {r.split(b"\t", 1)[1].decode(): r.split(b" ", 1)[0].decode() for r in out.split(b"\0") if r}
top = os.path.realpath(".")
def read(path, root):  # a tracked regular file under root, never through a link, never blocking
    if modes.get(path) not in ("100644", "100755"):
        die(f"{path} is not a tracked regular file", "pin only tracked regular files")
    if os.path.commonpath([os.path.realpath(root), os.path.realpath(path)]) != os.path.realpath(root):
        die(f"{path} resolves outside {root}", "pin only files inside the fixture roots")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        die(f"{path} is pinned but missing", REMINT)
    except OSError:
        die(f"{path} is a symlink or cannot be opened", "pin only regular files")
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            die(f"{path} is not a regular file", "pin only regular files")
        return f.read()
REMINT = "restore the bytes, or re-mint the fixture and its manifest together for review"
manifests = [m for m in modes if m.startswith("tests/fixtures/") and (P(m).name == "manifest.json" or m.endswith(".meta.json"))]
if not manifests:
    die("no pinned fixture manifests under tests/fixtures (an empty check set is not green)", "restore the manifests")
pines, pins = [p for p in modes if p.startswith("research/pine/")], []
for m in manifests:
    try:
        doc = json.loads(read(m, "tests/fixtures"))
    except ValueError:
        die(f"{m} is not readable JSON")
    if not isinstance(doc, dict):
        die(f"{m} is not a JSON object")
    if P(m).name == "manifest.json":
        files = doc.get("files")
        if not isinstance(files, dict) or not files:
            die(f"{m} has no files map")
        for name, digest in files.items():
            if P(name).is_absolute() or ".." in P(name).parts:
                die(f"{m}: {name!r} must be a relative path under {P(m).parent}")
            pins.append((m, str(P(m).parent / name), digest, "tests/fixtures"))
    for key, digest in doc.items():
        if not key.endswith("_sha256") or key == "input_config_sha256":
            continue
        if key == "trace_sha256" and m.endswith(".meta.json"):
            pins.append((m, m[: -len(".meta.json")] + ".csv", digest, "tests/fixtures"))
        elif key == "pine_sha256" and m.endswith(".meta.json"):
            found = [p for p in pines if re.fullmatch(re.escape(f"{doc.get('catalog_number')}_") + r"[^/]*\.pine", P(p).name)]
            if len(found) != 1:
                die(f"{m}: pine_sha256 needs exactly one tracked research/pine/{doc.get('catalog_number')}_*.pine, found {len(found)}")
            pins.append((m, found[0], digest, "research/pine"))
        elif not (key == "owner_attestation_sha256" and digest is None):
            die(f"{m}: unmapped pin {key} (no file binding is known for it)")
for m, path, digest, root in pins:
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        die(f"{m}: the digest for {path} is not a lowercase sha256")
    if path not in modes and not os.path.lexists(path):
        die(f"{path} is pinned but missing (pinned by {m})", REMINT)
    actual = hashlib.sha256(read(path, root)).hexdigest()
    if actual != digest:
        die(f"{path} sha256 mismatch: {m} pins {digest}, the bytes hash to {actual}", REMINT)
print(f"{len(pins)} pinned files in {len(manifests)} manifests match their sha256")
PY
}
if out="$(verify 2>&1)"; then echo "PASS: $g — $out"; else echo "FAIL: $g — $(tail -n 1 <<< "$out")" >&2; exit 1; fi
