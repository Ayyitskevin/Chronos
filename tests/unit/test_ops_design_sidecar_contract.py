"""docs/ops/DESIGN-alert-sidecar.md says what is (D-1 review, 2026-09-15).

The design names local producers and an anchor schema. This contract holds the document to
the tree: a producer row marked ``present`` must exist as a module, a row marked ``planned``
must NOT exist yet (when W-1/R-1 land, the row flips and this test says so), and the anchor
bytes the document quotes must be the bytes ``chronos.auditlog.log._anchor_bytes`` writes.
The design must also never describe a sidecar-to-host call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from chronos.auditlog.log import _anchor_bytes

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "ops" / "DESIGN-alert-sidecar.md"

_ROW = re.compile(
    r"^\| `(?P<kind>[\w.]+)` \| (?P<producer>.*?) \| (?P<status>present|planned(?: \([^)]*\))?) \|",
    re.M,
)
_MODULE = re.compile(r"`chronos\.operations\.(\w+)`")


def _rows() -> list[dict[str, str]]:
    rows = [m.groupdict() for m in _ROW.finditer(DESIGN.read_text(encoding="utf-8"))]
    assert len(rows) == 6, rows  # the six record kinds the design names
    return rows


def test_a_present_producer_exists_and_a_planned_one_does_not_yet() -> None:
    for row in _rows():
        modules = _MODULE.findall(row["producer"])
        if row["kind"] == "audit.head":
            assert row["status"] == "present"
            assert (ROOT / "src" / "chronos" / "auditlog" / "log.py").exists()
            continue
        assert modules, row
        for name in modules:
            exists = (ROOT / "src" / "chronos" / "operations" / f"{name}.py").exists()
            if row["status"].startswith("planned"):
                assert not exists, f"{name} exists now — flip the design row to `present`"
            else:
                assert exists, f"{name} is marked present but does not exist"


def test_the_quoted_anchor_bytes_are_the_bytes_the_log_writes() -> None:
    text = DESIGN.read_text(encoding="utf-8")
    quoted = re.search(r"its bytes are exactly `(\{[^`]*\})`", text)
    assert quoted, "the design must quote the anchor object"
    expected = json.loads(_anchor_bytes(7, "h" * 64).decode("utf-8"))
    quoted_keys = set(json.loads(quoted.group(1)).keys())
    assert quoted_keys == set(expected.keys()) == {"count", "last_hash"}
    assert "written_at" not in quoted.group(1)


@pytest.mark.parametrize("forbidden", ["/v1/commands", "sidecar tells the host", "remote restart"])
def test_the_design_refuses_a_path_into_the_host(forbidden: str) -> None:
    text = DESIGN.read_text(encoding="utf-8")
    # each phrase may appear only on a line that refuses it ("no" / "never" / "refuses" / "not")
    for line in text.splitlines():
        if forbidden in line:
            assert re.search(r"\b(no|never|refuses|not)\b", line, re.I), line


def test_time_authority_and_record_identity_are_defined() -> None:
    text = DESIGN.read_text(encoding="utf-8")
    assert "record_id = sha256(canonical)" in text
    assert 'sort_keys=True, separators=(",", ":")' in text
    assert "ONLY time authority" in text and "sidecar-side dead-man" in text
    # a public-key receiver verifies a signature; it recomputes only the digest (D-1 r1 review)
    assert "VERIFIES `signature` against the host's PUBLIC key" in text
    assert "the receiver never recomputes it" in text
    assert "64 lowercase" in text and "base64url" in text
