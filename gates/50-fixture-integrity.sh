#!/usr/bin/env bash
# 50-fixture-integrity.sh — pinned fixtures verify against their bytes; no silent drift.
# chronos has no SHA256SUMS files: this is the `sha256sum -c` equivalent over every tracked
# tests/fixtures/**/manifest.json (its "files" map) and tests/fixtures/**/*.meta.json (trace_sha256 →
# the sibling <stem>.csv; pine_sha256 → the one tracked research/pine/<catalog_number>_*.pine).
# input_config_sha256 hashes an in-document object (the loader's check); other *_sha256 keys, non-null attestations fail.
set -uo pipefail
g=fixture-integrity
top="$(git rev-parse --show-toplevel 2>/dev/null)" && cd "$top" || { echo "FAIL: $g — not inside a git work tree; run the gate from the repo root" >&2; exit 1; }
verify() { python3 - <<'PY'
import hashlib, json, re, subprocess, sys
from pathlib import PurePosixPath as P
def tracked(*spec):
    r = subprocess.run(["git", "ls-files", "-z", "--", *spec], capture_output=True, check=True)
    return [p.decode() for p in r.stdout.split(b"\0") if p]
def die(msg, hint="fix the manifest, then re-run"):
    print(f"{msg}; {hint}"); sys.exit(1)
REMINT = "restore the bytes, or re-mint the fixture and its manifest together for review"
manifests = [m for m in tracked("tests/fixtures") if P(m).name == "manifest.json" or m.endswith(".meta.json")]
if not manifests:
    die("no pinned fixture manifests under tests/fixtures (an empty check set is not green)", "restore the manifests")
pines, pins = tracked("research/pine"), []
for m in manifests:
    try:
        doc = json.load(open(m, encoding="utf-8"))
    except (OSError, ValueError):
        die(f"{m} is not readable JSON")
    if P(m).name == "manifest.json":
        files = doc.get("files")
        if not isinstance(files, dict) or not files:
            die(f"{m} has no files map")
        for name, digest in files.items():
            if P(name).is_absolute() or ".." in P(name).parts:
                die(f"{m}: {name!r} must be a relative path under {P(m).parent}")
            pins.append((m, str(P(m).parent / name), digest))
    for key, digest in doc.items():
        if not key.endswith("_sha256") or key == "input_config_sha256":
            continue
        if key == "trace_sha256" and m.endswith(".meta.json"):
            pins.append((m, m[: -len(".meta.json")] + ".csv", digest))
        elif key == "pine_sha256" and m.endswith(".meta.json"):
            found = [p for p in pines if re.fullmatch(re.escape(f"{doc.get('catalog_number')}_") + r"[^/]*\.pine", P(p).name)]
            if len(found) != 1:
                die(f"{m}: pine_sha256 needs exactly one tracked research/pine/{doc.get('catalog_number')}_*.pine, found {len(found)}")
            pins.append((m, found[0], digest))
        elif not (key == "owner_attestation_sha256" and digest is None):
            die(f"{m}: unmapped pin {key} (no file binding is known for it)")
for m, path, digest in pins:
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        die(f"{m}: the digest for {path} is not a lowercase sha256")
    try:
        actual = hashlib.sha256(open(path, "rb").read()).hexdigest()
    except FileNotFoundError:
        die(f"{path} is pinned but missing (pinned by {m})", REMINT)
    if actual != digest:
        die(f"{path} sha256 mismatch: {m} pins {digest}, the bytes hash to {actual}", REMINT)
print(f"{len(pins)} pinned files in {len(manifests)} manifests match their sha256")
PY
}
if out="$(verify 2>&1)"; then echo "PASS: $g — $out"; else echo "FAIL: $g — $out" >&2; exit 1; fi
