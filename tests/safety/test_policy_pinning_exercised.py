"""A4, exercised: the policy file's bytes become the registration's label.

R-47 residual (b): `prompt_version` is an owner-typed label, so nothing binds it
to the policy the worker runs. Edit `worker/policy.md`, and unless the owner
remembers to bump the label by hand, every proposal still cites the same
"version" — so a mandate pinned to that version keeps agreeing across an edit it
never saw, and two runs under materially different strategy instructions are
indistinguishable in the journal.

`--policy-file` derives the label instead, and these tests pin both halves of
what that is worth:

1. **What it does.** The label IS the file's digest, byte-sensitive: a
   one-character edit changes it, so an edited policy stops matching a mandate's
   pin and admission refuses until the owner re-pins deliberately.
2. **What it does not.** It binds the file **as it was at mint time**. It does
   not prove which policy the worker *ran* — nothing in Chronos observes that
   read. `check --policy-file` narrows this to drift detection ("the file on
   this machine no longer matches what was registered"), which is the strongest
   claim available without the worker attesting its own digest.

Also pinned, because a mint-time-only guarantee decays silently: the drift
comparison must *fire*, not merely exist — a DIFFERS that never appears is the
inert-control shape (R-25..R-27) in a reporting tool.

Every command here still writes nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from chronos.cli.proposer_commands import (
    POLICY_DIGEST_CHARS,
    cmd_proposer_check,
    cmd_proposer_fingerprint,
    cmd_proposer_mint,
    policy_fingerprint,
)
from chronos.supervisor.proposers import credential_hash

_POLICY = "You are a trading analyst. Propose HOLD unless the evidence is overwhelming.\n"
_FAR_EXPIRY = "2030-01-01T00:00:00+00:00"


def _policy_file(tmp_path: Path, text: str = _POLICY) -> Path:
    path = tmp_path / "policy.md"
    path.write_text(text, encoding="utf-8")
    return path


def _mint_args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "proposer_id": "claude-worker",
        "provider": "anthropic",
        "model_id": "claude-opus-5",
        "model_version": "1",
        "prompt_version": "1",
        "tool_schema_version": "1",
        "decision_schema_version": "1",
        "policy_version": "1",
        "expires_at": "",
        "expires_days": 90,
        "policy_file": "",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _minted_entry(out: str) -> dict[str, Any]:
    """The registry document mint printed, parsed out of its surrounding prose.

    ``raw_decode`` rather than ``json.loads`` because mint prints explanatory
    lines after the document when a policy file was used — the honest bound
    travels with the output, and a parser that assumed the JSON ran to the end
    of stdout would break on exactly the case this suite exists to test.
    """

    document, _ = json.JSONDecoder().raw_decode(out[out.rindex('{\n  "schema_version"') :])
    entry: dict[str, Any] = document["proposers"][0]
    return entry


def _registry_text(**entry_overrides: Any) -> str:
    entry: dict[str, Any] = {
        "proposer_id": "claude-worker",
        "secret_sha256": credential_hash("w" * 64),
        "provider": "anthropic",
        "model_id": "claude-opus-5",
        "model_version": "1",
        "prompt_version": "1",
        "tool_schema_version": "1",
        "decision_schema_version": "1",
        "policy_version": "1",
        "expires_at": _FAR_EXPIRY,
        "enabled": True,
    }
    entry.update(entry_overrides)
    return json.dumps({"schema_version": 1, "proposers": [entry]})


# ------------------------------------------------------------ the derivation


def test_the_label_is_the_policy_digest_and_is_byte_sensitive(tmp_path: Path) -> None:
    """One character changes the label. That is the whole mechanism.

    Asserted against an independently computed SHA-256 rather than against the
    function's own output, so a derivation that silently changed (a different
    hash, a decoded-and-stripped read, a longer truncation) fails here instead
    of agreeing with itself.
    """

    path = _policy_file(tmp_path)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()[:POLICY_DIGEST_CHARS]
    assert policy_fingerprint(path) == expected
    assert len(policy_fingerprint(path)) == POLICY_DIGEST_CHARS

    edited = _policy_file(tmp_path, _POLICY.replace("overwhelming", "overwhelmingly clear"))
    assert policy_fingerprint(edited) != expected

    # Whitespace is content: the digest is over raw bytes, not stripped text,
    # because a rule that forgave some byte edits and not others is a rule
    # nobody can apply from memory.
    trailing = _policy_file(tmp_path, _POLICY + "\n")
    assert policy_fingerprint(trailing) != expected


def test_minting_with_a_policy_file_stamps_the_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The registration the owner pastes carries the derived label, not "1"."""

    path = _policy_file(tmp_path)
    assert cmd_proposer_mint(_mint_args(policy_file=str(path))) == 0
    out = capsys.readouterr().out
    entry = _minted_entry(out)

    assert entry["prompt_version"] == policy_fingerprint(path)
    assert entry["prompt_version"] != "1"
    # The honest bound travels with the output, where the owner reads it.
    assert "does NOT prove" in out
    assert "which policy the worker actually ran" in out


def test_minting_without_a_policy_file_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The typed label still works; A4 adds an option, it does not impose one."""

    assert cmd_proposer_mint(_mint_args(prompt_version="pv-7")) == 0
    out = capsys.readouterr().out
    assert _minted_entry(out)["prompt_version"] == "pv-7"
    assert "does NOT prove" not in out, "no policy claim is made when none was asked for"


# ------------------------------------------------------- the refusing shapes


def test_a_typed_label_and_a_policy_file_together_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two authorities for one field is worse than none.

    argparse refuses this at the command line; this pins the same refusal on the
    function itself, because tests and scripts call it directly and a
    precedence rule ("the digest wins") would make a registration's provenance
    depend on which flag someone remembered.
    """

    path = _policy_file(tmp_path)
    assert cmd_proposer_mint(_mint_args(policy_file=str(path), prompt_version="pv-7")) == 2
    assert "not both" in capsys.readouterr().err


def test_an_empty_policy_file_is_refused_not_digested(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty policy is not a strategy — the worker's own words.

    Digesting emptiness would mint a credential claiming a policy the worker
    refuses to start on, putting a fiction in the registry.
    """

    empty = _policy_file(tmp_path, "   \n\n")
    with pytest.raises(ValueError, match="empty"):
        policy_fingerprint(empty)
    assert cmd_proposer_mint(_mint_args(policy_file=str(empty))) == 2
    assert "empty" in capsys.readouterr().err


def test_an_unreadable_policy_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "absent.md"
    with pytest.raises(ValueError, match="unreadable"):
        policy_fingerprint(missing)
    assert cmd_proposer_mint(_mint_args(policy_file=str(missing))) == 2
    assert "unreadable" in capsys.readouterr().err


# --------------------------------------------------------------- fingerprint


def test_fingerprint_prints_exactly_what_mint_would_derive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-pinning a mandate must not require rotating a credential.

    Proven by comparing the two commands' outputs over the same bytes rather
    than by restating the formula in the test — two implementations of one rule
    is how they drift.
    """

    path = _policy_file(tmp_path)
    assert cmd_proposer_fingerprint(argparse.Namespace(policy_file=str(path))) == 0
    printed = capsys.readouterr().out
    derived = next(
        line.split(":", 1)[1].strip()
        for line in printed.splitlines()
        if line.startswith("prompt_version:")
    )

    assert cmd_proposer_mint(_mint_args(policy_file=str(path))) == 0
    assert _minted_entry(capsys.readouterr().out)["prompt_version"] == derived
    assert "does NOT prove" in printed


def test_fingerprint_refuses_an_unusable_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cmd_proposer_fingerprint(argparse.Namespace(policy_file=str(tmp_path / "gone.md"))) == 2
    assert "unreadable" in capsys.readouterr().err


# ------------------------------------------------------------- drift, at check


def test_check_reports_matches_then_differs_after_an_edit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The drift comparison must actually fire — both verdicts, on real bytes.

    This is the only part of A4 that keeps working after mint time, and a
    reporting control that never produces its negative verdict is the
    R-25..R-27 shape in a different costume.
    """

    policy = _policy_file(tmp_path)
    registry = tmp_path / "proposers.json"
    registry.write_text(_registry_text(prompt_version=policy_fingerprint(policy)), encoding="utf-8")

    assert cmd_proposer_check(argparse.Namespace(file=str(registry), policy_file=str(policy))) == 0
    matched = capsys.readouterr().out
    assert "policy MATCHES" in matched

    policy.write_text(_POLICY.replace("HOLD", "OPEN"), encoding="utf-8")
    assert cmd_proposer_check(argparse.Namespace(file=str(registry), policy_file=str(policy))) == 0
    drifted = capsys.readouterr().out
    assert "policy DIFFERS" in drifted
    assert "re-mint" in drifted


def test_check_without_a_policy_file_claims_nothing_about_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silence, not a guess. An untested agreement must not be reported as one."""

    registry = tmp_path / "proposers.json"
    registry.write_text(_registry_text(), encoding="utf-8")
    assert cmd_proposer_check(argparse.Namespace(file=str(registry), policy_file="")) == 0
    out = capsys.readouterr().out
    assert "MATCHES" not in out
    assert "DIFFERS" not in out
    assert "claude-worker" in out  # it still did its original job


def test_check_reports_an_unreadable_policy_without_claiming_agreement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "proposers.json"
    registry.write_text(_registry_text(), encoding="utf-8")
    assert (
        cmd_proposer_check(
            argparse.Namespace(file=str(registry), policy_file=str(tmp_path / "gone.md"))
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "UNREADABLE" in out
    assert "MATCHES" not in out


# --------------------------------------------------------- still writes nothing


def test_no_policy_command_writes_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool that mints text is not a tool that grants — unchanged by A4."""

    policy = _policy_file(tmp_path)
    registry = tmp_path / "proposers.json"
    registry.write_text(_registry_text(), encoding="utf-8")
    before_registry = registry.read_bytes()
    before_policy = policy.read_bytes()
    monkeypatch.chdir(tmp_path)
    before_tree = set(tmp_path.iterdir())

    cmd_proposer_mint(_mint_args(policy_file=str(policy)))
    cmd_proposer_fingerprint(argparse.Namespace(policy_file=str(policy)))
    cmd_proposer_check(argparse.Namespace(file=str(registry), policy_file=str(policy)))
    capsys.readouterr()

    assert set(tmp_path.iterdir()) == before_tree
    assert registry.read_bytes() == before_registry
    assert policy.read_bytes() == before_policy, "reading a policy must never rewrite it"


def test_check_reports_revocation_and_policy_drift_together(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The interaction A3 and A4 create jointly, which neither built alone.

    A3 taught `check` to report REVOKED per registration; A4 taught it to report
    whether the policy on this machine still matches what was registered. They
    were developed on sibling branches and merged here, so this is the first
    test that runs both reporting paths over the same entry — the shape where a
    merge quietly drops one feature's output, or lets one's failure suppress the
    other's.

    Both must appear, and they must stay independent: a revoked credential whose
    policy has ALSO drifted is two separate facts an incident review needs, and
    the state column must still read REVOKED rather than being overwritten by
    the policy verdict.
    """

    from chronos.cli.proposer_commands import cmd_proposer_revoke
    from chronos.persistence.database import Database

    policy = _policy_file(tmp_path)
    registry = tmp_path / "proposers.json"
    registry.write_text(_registry_text(prompt_version=policy_fingerprint(policy)), encoding="utf-8")

    url = f"sqlite:///{tmp_path / 'ledger.db'}"
    database = Database(url)
    try:
        database.initialize()
    finally:
        database.dispose()

    entry = json.loads(registry.read_text(encoding="utf-8"))["proposers"][0]
    assert (
        cmd_proposer_revoke(
            argparse.Namespace(
                file=str(registry),
                proposer_id=entry["proposer_id"],
                reason="credential leaked and the policy moved on",
                database_url=url,
            )
        )
        == 0
    )
    capsys.readouterr()

    # The policy drifts after the revocation, so both facts are true at once.
    policy.write_text(_POLICY.replace("HOLD", "OPEN"), encoding="utf-8")
    assert (
        cmd_proposer_check(
            argparse.Namespace(file=str(registry), policy_file=str(policy), database_url=url)
        )
        == 0
    )
    out = capsys.readouterr().out

    entry_line = next(
        line for line in out.splitlines() if line.startswith(f"  {entry['proposer_id']}")
    )
    assert "REVOKED" in entry_line, "A3's state must survive A4's reporting"
    assert "CURRENT" not in entry_line
    assert "credential leaked and the policy moved on" in out, "A3's reason line must survive"
    assert "policy DIFFERS" in out, "A4's drift verdict must survive"
