"""Offline replay check for captured real-gateway session directories.

Measures the VCP section 7 EXIT criterion "captured fixtures replay offline
exactly" for the slice this campaign captures:

1. Byte integrity: every file listed in manifest.json still hashes to the
   recorded sha256 (the fixture has not drifted since capture).
2. Derivation replay: re-derives derived_liquid_hours.json from the RAW strings
   in capture.json using the same repo code path (parse_liquid_hours +
   confirms_open at the captured probe instant) and byte-compares the result.
3. Mutation scan: asserts no order-mutation step name appears in the capture.

No network. No broker. Exit code 0 only when every check passes for every
session directory given.

Usage:

    .venv/bin/python .claude/skills/chronos-real-gateway-campaign/scripts/\
replay_check.py fixtures/ibkr/2026-08-XX-session-1 [more session dirs...]
"""

from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_readonly import canonical_json, derive_liquid_hours  # noqa: E402

_MUTATION_TOKENS = ("submit_order", "preview_order", "modify_order", "cancel_order")


def check_session(directory: Path) -> list[str]:
    failures: list[str] = []
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return [f"{directory}: manifest.json missing"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for name, expected in manifest.get("files", {}).items():
        path = directory / name
        if not path.is_file():
            failures.append(f"{directory}: listed file missing: {name}")
            continue
        actual = sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            failures.append(f"{directory}: sha256 drift in {name}: {actual} != {expected}")

    capture_path = directory / "capture.json"
    derived_path = directory / "derived_liquid_hours.json"
    if capture_path.is_file() and derived_path.is_file():
        capture = json.loads(capture_path.read_text(encoding="utf-8"))
        recomputed = canonical_json(derive_liquid_hours(capture))
        stored = derived_path.read_text(encoding="utf-8")
        if recomputed != stored:
            failures.append(
                f"{directory}: derived_liquid_hours.json does NOT replay byte-exactly "
                "(repo parser output changed, or the fixture was hand-edited; "
                "record the divergence — do not edit the fixture to match)"
            )
        for step_name in capture.get("steps", {}):
            if any(token in step_name for token in _MUTATION_TOKENS):
                failures.append(f"{directory}: mutation-shaped step in capture: {step_name}")
    else:
        failures.append(f"{directory}: capture.json or derived_liquid_hours.json missing")

    if not manifest.get("gateway_evidence", False):
        failures.append(
            f"{directory}: manifest says gateway_evidence=false (demo rehearsal) — "
            "this directory cannot count toward the section 7 gate"
        )
    return failures


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    all_failures: list[str] = []
    for argument in sys.argv[1:]:
        directory = Path(argument)
        failures = check_session(directory)
        status = "PASS" if not failures else "FAIL"
        print(f"[{status}] {directory}")
        all_failures.extend(failures)
    for failure in all_failures:
        print(f"  {failure}")
    return 0 if not all_failures else 1


if __name__ == "__main__":
    sys.exit(main())
