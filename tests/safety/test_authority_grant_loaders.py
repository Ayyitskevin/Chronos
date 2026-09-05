"""The owner-authored grants are read under the GRANT contract (R-71).

The mandate and the proposer registry decide what this system is authorized to
do and who may ask it to. Until ADR-0053 both were read with
``path.read_bytes()``: a symlink was followed, the path was re-resolved between
any check and the read, and the mode was never looked at — so a mandate a
second account could rewrite was honoured without complaint.

They now go through ``read_authority_file`` in ``AuthorityMode.GRANT``. The
mode rule is deliberately *not* the token's: a grant is not a secret, so what
matters is that nobody else could have **written** it. ``0644`` is accepted
because that is what ``chronos.cli mandate template > file`` produces; group-
or other-writable is refused because anyone in that set can change the grant.

These tests pin the loaders' behaviour. The helper's own contract — both modes,
positively and negatively, and the proof they cannot collapse into one another
— lives in ``test_authority_file_contract.py``.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from chronos.api.auth import load_proposer_auth
from chronos.api.autonomy_wiring import load_persistent_mandate
from chronos.supervisor.proposers import load_proposer_registry
from chronos.utils.secure_files import UnsafeAuthorityFile

_REGISTRY = {"schema_version": 1, "proposers": []}
#: A string that can only have come from reading a symlink's target.
_DECOY = "decoy-bytes-the-loader-must-never-read"


def _grant(path: Path, payload: str, mode: int) -> Path:
    """An owner-authored grant document at an exact mode, umask notwithstanding."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, mode)
    return path


@contextmanager
def _wiring_logs() -> Iterator[list[logging.LogRecord]]:
    """Capture the loader's own logger.

    ``caplog`` handles the root logger and ``configure_logging`` sets
    ``propagate = False`` on ``chronos`` process-wide, so a root-level capture
    is empty once any earlier test has configured logging — and an assertion
    about an absent phrase then passes for the wrong reason.
    """

    collected: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            collected.append(record)

    logger = logging.getLogger("chronos.api.autonomy")
    handler = _Collector(level=logging.DEBUG)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield collected
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _mandate_json() -> str:
    """The smallest document the mandate model accepts is not needed here.

    Every test below either expects a refusal *before* validation or asserts
    that validation is what refused — so an invalid body is enough, and keeping
    it invalid means these tests do not have to track the mandate schema.
    """

    return json.dumps({"not": "a valid mandate"})


# ------------------------------------------------------------------ the registry


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_a_registry_nobody_else_can_write_is_read(tmp_path: Path, mode: int) -> None:
    path = _grant(tmp_path / "proposers.json", json.dumps(_REGISTRY), mode)
    loaded = load_proposer_registry(path)
    assert loaded is not None
    assert loaded.registry.schema_version == 1


@pytest.mark.parametrize("mode", [0o664, 0o666, 0o622])
def test_a_registry_anyone_else_can_write_is_refused(tmp_path: Path, mode: int) -> None:
    """Whoever can write this file decides who may propose."""

    path = _grant(tmp_path / "proposers.json", json.dumps(_REGISTRY), mode)
    with pytest.raises(UnsafeAuthorityFile, match="writable by"):
        load_proposer_registry(path)
    assert stat.S_IMODE(path.stat().st_mode) == mode, "refused, never repaired"


def test_a_symlinked_registry_is_refused_and_the_target_is_never_read(
    tmp_path: Path,
) -> None:
    target = _grant(tmp_path / "real.json", json.dumps(_REGISTRY), 0o644)
    link = tmp_path / "proposers.json"
    link.symlink_to(target)
    with pytest.raises(UnsafeAuthorityFile, match="symlink"):
        load_proposer_registry(link)


def test_an_absent_registry_still_returns_none(tmp_path: Path) -> None:
    """The pre-existing contract: absence is a state every caller handles."""

    assert load_proposer_registry(tmp_path / "missing.json") is None


def test_an_invalid_registry_still_returns_none(tmp_path: Path) -> None:
    """ "Does not parse" and "may not be trusted" stay distinguishable."""

    path = _grant(tmp_path / "proposers.json", "{ not json", 0o644)
    assert load_proposer_registry(path) is None


def test_the_registry_digest_is_over_the_bytes_that_were_read(tmp_path: Path) -> None:
    import hashlib

    payload = json.dumps(_REGISTRY)
    path = _grant(tmp_path / "proposers.json", payload, 0o644)
    loaded = load_proposer_registry(path)
    assert loaded is not None
    assert loaded.digest == hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ----------------------------------------------------- the posture the app sees


def test_an_unsafe_registry_reaches_startup_as_unsafe_not_merely_invalid(
    tmp_path: Path,
) -> None:
    """The distinction main.py needs to raise a typed fault rather than one word.

    Both postures refuse every proposal. Only one of them means someone else
    could have written the document that says who may propose.
    """

    path = _grant(tmp_path / "proposers.json", json.dumps(_REGISTRY), 0o664)
    auth = load_proposer_auth(path)
    assert auth.configured is True
    assert auth.registry is None
    assert auth.unsafe is True


def test_an_invalid_registry_is_configured_but_not_unsafe(tmp_path: Path) -> None:
    path = _grant(tmp_path / "proposers.json", "{ not json", 0o644)
    auth = load_proposer_auth(path)
    assert auth.configured is True
    assert auth.registry is None
    assert auth.unsafe is False, "a typo in a grant is not an untrustworthy file"


def test_no_registry_configured_is_neither(tmp_path: Path) -> None:
    auth = load_proposer_auth(None)
    assert auth.configured is False
    assert auth.unsafe is False


# ------------------------------------------------------------------- the mandate


def test_a_mandate_anyone_else_can_write_is_refused(tmp_path: Path) -> None:
    """Autonomy stays inert rather than honouring a grant a second account owns."""

    path = _grant(tmp_path / "mandate.json", _mandate_json(), 0o664)
    assert load_persistent_mandate(path) is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o664, "refused, never repaired"


def test_a_symlinked_mandate_is_refused_and_the_target_is_never_read(
    tmp_path: Path,
) -> None:
    """Which refusal fired is the whole claim — the return value proves nothing.

    ``_mandate_json()`` fails validation, so ``None`` comes back whether the
    symlink was refused or followed-and-then-rejected. Asserting only ``None``
    made this test survive removing ``O_NOFOLLOW`` outright. The registry twin
    above always checked this properly; the mandate half did not.
    """

    target = _grant(tmp_path / "real.json", json.dumps({"decoy": _DECOY}), 0o644)
    link = tmp_path / "mandate.json"
    link.symlink_to(target)

    with _wiring_logs() as logged:
        assert load_persistent_mandate(link) is None

    text = "\n".join(record.getMessage() for record in logged)
    assert logged, "nothing was logged; the assertions below would be vacuous"
    assert "unsafe" in text, "the symlink must be refused, not followed and then rejected"
    assert _DECOY not in text, "the target's bytes must never be read"
    assert "does not validate" not in text, "validation was reached, so the link was followed"


def test_a_mandate_at_0644_is_read_and_refused_by_validation_not_by_mode(
    tmp_path: Path,
) -> None:
    """0644 is what the documented creation path produces; it must not be refused.

    Both outcomes are ``None``, so the return value proves nothing on its own —
    which refusal *fired* is the whole claim. At 0644 the read succeeds and
    validation rejects the body; at 0664 the read never happens.
    """

    path = _grant(tmp_path / "mandate.json", _mandate_json(), 0o644)
    with _wiring_logs() as accepted:
        assert load_persistent_mandate(path) is None
    accepted_text = "\n".join(record.getMessage() for record in accepted)
    assert accepted, "nothing was logged; the assertions below would be vacuous"
    assert "does not validate" in accepted_text
    assert "unsafe" not in accepted_text, "0644 must not trip the mode check"

    widened = _grant(tmp_path / "widened.json", _mandate_json(), 0o664)
    with _wiring_logs() as refused:
        assert load_persistent_mandate(widened) is None
    refused_text = "\n".join(record.getMessage() for record in refused)
    assert "unsafe" in refused_text
    assert "does not validate" not in refused_text, "the body was never reached"


def test_an_absent_mandate_is_inert_not_a_crash(tmp_path: Path) -> None:
    assert load_persistent_mandate(tmp_path / "missing.json") is None


def test_a_non_utf8_mandate_refuses_instead_of_raising(tmp_path: Path) -> None:
    path = tmp_path / "mandate.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, b"\xff\xfe not text")
    finally:
        os.close(descriptor)
    os.chmod(path, 0o644)
    assert load_persistent_mandate(path) is None
