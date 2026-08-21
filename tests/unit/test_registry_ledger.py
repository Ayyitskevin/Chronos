"""Experiment-registry ledger + trial-count + budget tests (C2, ADR-0013 §1/§2/§5/§6)."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import chronos.registry.ledger as ledger_module
from chronos.registry import (
    CANONICAL_REGISTRY_LEDGER_PATH,
    KIND_UNLOCK,
    RegistryIntegrityError,
    RegistryLedger,
    RunStage,
    accrued_capture_sessions,
    available_budget,
    data_fingerprint,
    register_run,
    trial_count,
)
from chronos.registry.ledger import registry_lock, verified_registry_records
from chronos.research.five_tool_trials import registered_trial_count

_NOW = datetime(2026, 7, 20, 21, 0, tzinfo=UTC)


def _ledger(tmp_path: Path) -> RegistryLedger:
    return RegistryLedger(tmp_path / "registry.jsonl")


def _run(ledger: RegistryLedger, strategy: str, *, touched: bool = True) -> None:
    register_run(
        ledger,
        stage=RunStage.DEV,
        strategy_id=strategy,
        config_hash="cfg",
        code_commit="abc123",
        data_hashes={"SPY": {"bars_sha": "b", "actions_sha": "a"}},
        criteria_ref="frozen@2026-01-01",
        touched_data=touched,
    )


def test_append_and_verify_chain(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    _run(ledger, "regime_trend_v1")
    assert len(ledger.records()) == 2
    ok, detail = ledger.verify()
    assert ok, detail


def test_stale_public_append_refreshes_under_verified_lock_without_duplicate_sequence(
    tmp_path: Path,
) -> None:
    first = _ledger(tmp_path)
    stale = _ledger(tmp_path)

    first_record = first.append("test_record", {"writer": "first"})
    refreshed_record = stale.append("test_record", {"writer": "formerly-stale"})

    assert (first_record.sequence, refreshed_record.sequence) == (0, 1)
    ledger = _ledger(tmp_path)
    assert ledger.verify()[0] is True
    assert [record.sequence for record in ledger.records()] == [0, 1]


def test_verified_records_validate_and_return_one_exact_ledger_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    ledger.append("test_record", {"value": "durable"})
    forged = RegistryLedger(tmp_path / "forged.jsonl")
    forged.append("test_record", {"value": "forged-middle-read"})
    forged_bytes = forged.path.read_bytes()
    ledger_reads = 0
    real_read_optional = ledger_module._RegistryDirectory.read_optional

    def staged_read(
        directory: ledger_module._RegistryDirectory,
        name: str,
    ) -> bytes | None:
        nonlocal ledger_reads
        if directory.capability.path == ledger.path and name == ledger.path.name:
            ledger_reads += 1
            if ledger_reads == 2:
                return forged_bytes
        return real_read_optional(directory, name)

    monkeypatch.setattr(ledger_module._RegistryDirectory, "read_optional", staged_read)

    records = verified_registry_records(ledger._path_capability)

    assert ledger_reads == 1
    assert [record.payload for record in records] == [{"value": "durable"}]


def test_verified_records_refuse_a_forged_ledger_snapshot_against_real_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    ledger.append("test_record", {"value": "durable"})
    forged = RegistryLedger(tmp_path / "forged.jsonl")
    forged.append("test_record", {"value": "forged-snapshot"})
    forged_bytes = forged.path.read_bytes()
    real_read_optional = ledger_module._RegistryDirectory.read_optional

    def forged_ledger_read(
        directory: ledger_module._RegistryDirectory,
        name: str,
    ) -> bytes | None:
        if directory.capability.path == ledger.path and name == ledger.path.name:
            return forged_bytes
        return real_read_optional(directory, name)

    monkeypatch.setattr(
        ledger_module._RegistryDirectory,
        "read_optional",
        forged_ledger_read,
    )

    with pytest.raises(RegistryIntegrityError, match="head hash mismatch"):
        verified_registry_records(ledger._path_capability)


def test_nested_registry_lock_on_the_same_path_does_not_deadlock(tmp_path: Path) -> None:
    """Same-thread re-entry must reuse the published directory.

    ``registered_trial_count`` used to take ``registry_lock`` and then call
    ``trial_count`` → ``verified_registry_records``, which took it again.
    ``threading.Lock`` is not reentrant, so the thread parked on a futex
    until CI's 10-minute job timeout cancelled the run.
    """

    path = tmp_path / "registry.jsonl"
    with registry_lock(path) as outer:
        with registry_lock(path) as inner:
            assert inner is outer
        assert registered_trial_count(path) == 0


@pytest.mark.parametrize(
    ("kind", "payload", "match"),
    [
        ("", {"value": 1}, "non-empty string"),
        (None, {"value": 1}, "non-empty string"),
        ("test_record", {"value": float("nan")}, "non-finite"),
        ("test_record", {"value": float("inf")}, "non-finite"),
        ("test_record", {"value": float("-inf")}, "non-finite"),
        ("test_record", {1: "coerced-key"}, "non-string object key"),
    ],
)
def test_append_rejects_noncanonical_input_without_changing_durable_bytes(
    tmp_path: Path,
    kind: object,
    payload: object,
    match: str,
) -> None:
    ledger = _ledger(tmp_path)
    ledger.append("test_record", {"value": "preserved"})
    ledger_before = ledger.path.read_bytes()
    anchor_before = ledger.anchor_path.read_bytes()

    with pytest.raises(ValueError, match=match):
        ledger.append(kind, payload)  # type: ignore[arg-type]

    assert ledger.path.read_bytes() == ledger_before
    assert ledger.anchor_path.read_bytes() == anchor_before
    assert ledger.verify()[0] is True


@pytest.mark.parametrize(
    "entry_name",
    ("registry.jsonl", "registry.jsonl.lock", "registry.head.json"),
)
def test_registry_refuses_ledger_lock_and_anchor_symlinks_without_touching_victim(
    tmp_path: Path,
    entry_name: str,
) -> None:
    parent = tmp_path / "registry"
    parent.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch", encoding="utf-8")
    (parent / entry_name).symlink_to(victim)

    with pytest.raises(RegistryIntegrityError, match="symlink"):
        RegistryLedger(parent / "registry.jsonl")

    assert victim.read_text(encoding="utf-8") == "do not touch"
    assert set(path.name for path in parent.iterdir()) == {entry_name}


def test_registry_refuses_a_symlink_parent_without_mutating_its_target(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    linked_parent = tmp_path / "linked-registry"
    linked_parent.symlink_to(victim, target_is_directory=True)

    with pytest.raises(RegistryIntegrityError, match="real directory"):
        RegistryLedger(linked_parent / "registry.jsonl")
    assert list(victim.iterdir()) == []


def test_registry_leaf_fifo_swap_is_opened_nonblocking_and_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    ledger.append("test_record", {"value": "preserved"})
    original = ledger.path.read_bytes()
    displaced = tmp_path / "registry.original"
    real_open = os.open
    attack_armed = True

    def swap_fifo_before_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal attack_armed
        if attack_armed and path == ledger.path.name and dir_fd is not None:
            attack_armed = False
            assert flags & os.O_NONBLOCK
            ledger.path.rename(displaced)
            os.mkfifo(ledger.path, mode=0o600)
            try:
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            finally:
                ledger.path.unlink()
                displaced.rename(ledger.path)
            return descriptor
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_fifo_before_open)

    with pytest.raises(RegistryIntegrityError, match="regular file"):
        ledger.records()
    assert attack_armed is False
    assert ledger.path.read_bytes() == original


def test_registry_refuses_parent_replacement_after_capability_construction(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "registry"
    displaced = tmp_path / "displaced"
    parent.mkdir()
    ledger = RegistryLedger(parent / "registry.jsonl")
    parent.rename(displaced)
    parent.mkdir()

    with pytest.raises(RegistryIntegrityError, match="replaced"):
        _run(ledger, "must-not-write")
    assert list(parent.iterdir()) == []
    assert list(displaced.iterdir()) == []


def test_anchor_publish_is_atomic_and_fsyncs_file_and_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacements: list[tuple[str, str, int | None, int | None]] = []
    fsynced_modes: list[int] = []
    real_replace = os.replace
    real_fsync = os.fsync

    def tracked_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        replacements.append((source, destination, src_dir_fd, dst_dir_fd))
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def tracked_fsync(descriptor: int) -> None:
        fsynced_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "replace", tracked_replace)
    monkeypatch.setattr(os, "fsync", tracked_fsync)
    ledger = _ledger(tmp_path)
    _run(ledger, "atomic-anchor")

    assert len(replacements) == 1
    source, destination, source_dir, destination_dir = replacements[0]
    assert source.startswith(".registry.head.json.") and source.endswith(".tmp")
    assert destination == "registry.head.json"
    assert source_dir is not None and source_dir == destination_dir
    assert any(stat.S_ISREG(mode) for mode in fsynced_modes)
    assert any(stat.S_ISDIR(mode) for mode in fsynced_modes)
    assert not list(tmp_path.glob(".*.tmp"))


def test_verify_detects_tampering(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    _run(ledger, "mean_reversion_v1")
    # Edit a committed record's payload in place — the chain must catch it.
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["payload"]["strategy_id"] = "tampered"
    lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _ = ledger.verify()
    assert ok is False


def test_verify_rejects_unhashed_top_level_record_extensions(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    row = json.loads(ledger.path.read_text(encoding="utf-8"))
    row["unhashed_extension"] = "must-not-be-ignored"
    ledger.path.write_text(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    ok, detail = ledger.verify()
    assert ok is False
    assert "keys do not match schema" in detail


def test_verify_rejects_duplicate_json_keys_even_when_the_effective_value_matches(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    line = ledger.path.read_text(encoding="utf-8").strip()
    ledger.path.write_text(line[:-1] + ',"kind":"experiment_run"}\n', encoding="utf-8")

    ok, detail = ledger.verify()
    assert ok is False
    assert "duplicate JSON key" in detail


@pytest.mark.parametrize("aliased_sequence", [False, 0.0])
def test_verify_rejects_bool_and_float_sequence_aliases(
    tmp_path: Path,
    aliased_sequence: object,
) -> None:
    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    row = json.loads(ledger.path.read_text(encoding="utf-8"))
    row["sequence"] = aliased_sequence
    ledger.path.write_text(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    ok, detail = ledger.verify()
    assert ok is False
    assert "true integer" in detail


def test_verify_rejects_unhashed_anchor_extensions_and_duplicate_keys(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    anchor = json.loads(ledger.anchor_path.read_text(encoding="utf-8"))
    anchor["unhashed_extension"] = True
    ledger.anchor_path.write_text(json.dumps(anchor) + "\n", encoding="utf-8")
    ok, detail = ledger.verify()
    assert ok is False
    assert "keys do not match schema" in detail

    ledger.anchor_path.write_text(
        '{"count":1,"last_hash":"' + str(anchor["last_hash"]) + '","count":1}\n',
        encoding="utf-8",
    )
    ok, detail = ledger.verify()
    assert ok is False
    assert "duplicate JSON key" in detail


def test_legacy_run_mutation_and_count_refuse_a_tampered_shared_ledger(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["payload"]["strategy_id"] = "tampered"
    ledger.path.write_text(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RegistryIntegrityError, match="verification"):
        _run(ledger, "another-strategy")
    with pytest.raises(RegistryIntegrityError, match="verification"):
        trial_count(ledger)


def test_trial_count_is_derived_and_scoped(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    _run(ledger, "regime_trend_v1")
    _run(ledger, "mean_reversion_v1")
    _run(ledger, "regime_trend_v1", touched=False)  # a non-data-touching run is not a trial
    assert trial_count(ledger) == 3
    assert trial_count(ledger, strategy_id="regime_trend_v1") == 2


def test_data_fingerprint_reads_bars_and_actions(tmp_path: Path) -> None:
    history = tmp_path / "research/data/history"
    history.mkdir(parents=True)
    (history / "MANIFEST.json").write_text(
        json.dumps(
            {
                "symbols": {
                    "SPY": {
                        "bars": {"sha256": "bars_hash"},
                        "corporate_actions": {"sha256": "actions_hash"},
                    },
                    "QQQ": {"bars": {"sha256": "qqq_bars"}},  # no actions file yet
                }
            }
        ),
        encoding="utf-8",
    )
    fp = data_fingerprint(history, ["SPY", "QQQ", "IWM"])
    assert fp["SPY"] == {
        "bars_sha": "bars_hash",
        "actions_sha": "actions_hash",
        "actions_captured": True,
    }
    # No actions file captured — a None sha is flagged, not mistaken for "no dividends".
    assert fp["QQQ"] == {"bars_sha": "qqq_bars", "actions_sha": None, "actions_captured": False}
    assert fp["IWM"] == {"bars_sha": None, "actions_sha": None, "actions_captured": False}


def test_verify_detects_tail_truncation(tmp_path: Path) -> None:
    # A bare hash chain can't catch tail deletion; the head anchor must (review F1).
    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    _run(ledger, "regime_trend_v1")
    _run(ledger, "mean_reversion_v1")
    assert ledger.verify()[0] is True
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    ledger.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")  # drop the last record
    ok, detail = ledger.verify()
    assert ok is False and "truncation" in detail
    # And whole-file deletion (anchor survives) is also caught.
    ledger.path.unlink()
    assert ledger.verify()[0] is False


def _budget(ledger: RegistryLedger, now: datetime, accrued: int = 40) -> int:
    return available_budget(
        ledger, now=now, accrued_sessions=accrued, sessions_per_unlock=20, max_outstanding_unlocks=2
    )


def test_budget_rations_against_accrual(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    assert _budget(ledger, _NOW, accrued=40) == 2  # 2 earned, 0 spent
    assert _budget(ledger, _NOW, accrued=0) == 0  # fail closed
    assert _budget(ledger, _NOW, accrued=200) == 2  # cap bounds it below earned


def test_budget_refunds_an_expired_unused_grant(tmp_path: Path) -> None:
    # An outstanding grant spends a credit; once it expires unused, the credit returns
    # (review F4 — an expired grant must not permanently burn a credit).
    ledger = _ledger(tmp_path)
    expires = (_NOW + timedelta(minutes=15)).isoformat()
    ledger.append(
        KIND_UNLOCK, {"unlock_id": "u1", "window": "w", "reason": "r", "expires_at": expires}
    )
    assert _budget(ledger, _NOW + timedelta(minutes=1)) == 1  # active grant spends one
    assert _budget(ledger, _NOW + timedelta(minutes=30)) == 2  # expired → refunded


def test_accrued_capture_sessions_counts_option_snapshots(tmp_path: Path) -> None:
    history = tmp_path / "history"
    history.mkdir()
    (history / "MANIFEST.json").write_text(
        json.dumps(
            {
                "symbols": {
                    "SPY": {"options": {"snapshots": {"2026-07-19": {}, "2026-07-20": {}}}},
                    "QQQ": {"options": {"snapshots": {"2026-07-20": {}}}},
                }
            }
        ),
        encoding="utf-8",
    )
    assert accrued_capture_sessions(history) == 3


# --- CLI wiring -------------------------------------------------------------


def test_cli_registry_stats_and_verify_are_fixed_to_the_canonical_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from chronos.cli.main import main

    monkeypatch.chdir(tmp_path)
    ledger = RegistryLedger(CANONICAL_REGISTRY_LEDGER_PATH)
    _run(ledger, "regime_trend_v1")
    assert main(["registry", "stats"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "canonical"
    assert payload["ledger"] == str(tmp_path / CANONICAL_REGISTRY_LEDGER_PATH)
    assert payload["trials"] == 1
    assert payload["chain_ok"] is True
    assert main(["registry", "verify"]) == 0

    with pytest.raises(SystemExit):
        main(["registry", "stats", "--ledger", str(tmp_path / "elsewhere.jsonl")])


def test_cli_caller_selected_registry_reporting_is_explicitly_legacy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from chronos.cli.main import main

    ledger = _ledger(tmp_path)
    _run(ledger, "regime_trend_v1")
    path = str(ledger.path)
    assert main(["registry", "legacy-stats", "--ledger", path]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "legacy_noncanonical"
    assert payload["legacy_trials"] == 1
    assert "trials" not in payload
    assert main(["registry", "legacy-verify", "--ledger", path]) == 0


def test_cli_canonical_stats_refuses_tamper_before_derived_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from chronos.cli.main import main

    monkeypatch.chdir(tmp_path)
    ledger = RegistryLedger(CANONICAL_REGISTRY_LEDGER_PATH)
    _run(ledger, "first")
    _run(ledger, "second")
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    ledger.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    assert main(["registry", "stats"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["scope"] == "canonical"
    assert "verification" in error["error"]


def test_cli_holdout_status_refuses_a_malformed_last_line_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from chronos.cli.main import main

    ledger = _ledger(tmp_path)
    _run(ledger, "first")
    ledger.path.write_text("{malformed-json\n", encoding="utf-8")

    assert (
        main(
            [
                "holdout",
                "status",
                "--ledger",
                str(ledger.path),
                "--history-root",
                str(tmp_path),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unreadable" in json.loads(captured.err)["error"]


def test_cli_holdout_status(tmp_path: Path) -> None:
    from chronos.cli.main import main

    ledger = _ledger(tmp_path)
    code = main(
        ["holdout", "status", "--ledger", str(ledger.path), "--history-root", str(tmp_path)]
    )
    assert code == 0


def test_cli_holdout_unlock_requires_env_phrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chronos.cli.main import main

    monkeypatch.delenv("CHRONOS_HOLDOUT_UNLOCK_PHRASE", raising=False)
    ledger = _ledger(tmp_path)
    code = main(
        [
            "holdout",
            "unlock",
            "--window",
            "w",
            "--reason",
            "r",
            "--ledger",
            str(ledger.path),
            "--history-root",
            str(tmp_path),
        ]
    )
    assert code == 2  # no phrase in env → refused before any ledger write
    assert ledger.records() == ()
