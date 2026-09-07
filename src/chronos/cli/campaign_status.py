"""Read-only status for the autonomy SHADOW campaign."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

_HEALTH_SNAPSHOT_MAX_AGE = timedelta(minutes=5)


class ConditionState(StrEnum):
    CLEAR = "CLEAR"
    TRIPPED = "TRIPPED"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True, slots=True)
class ConditionResult:
    name: str
    state: ConditionState
    section: str
    detail: str
    repair: str


def _read_only_database(path: Path) -> sqlite3.Connection:
    encoded = quote(str(path.resolve()), safe="/")
    return sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)


def _result(
    name: str,
    state: ConditionState,
    section: str,
    detail: str,
    repair: str,
) -> ConditionResult:
    return ConditionResult(name=name, state=state, section=section, detail=detail, repair=repair)


def _registry_posture(loaded: Any, *, path: Path, now: datetime) -> Any:
    from chronos.cli.mandate_check import RegisteredPins, RegistryPosture

    labels = (
        "provider",
        "model_id",
        "model_version",
        "prompt_version",
        "tool_schema_version",
        "decision_schema_version",
        "policy_version",
    )
    return RegistryPosture(
        path=str(path),
        proposers=tuple(
            RegisteredPins(
                proposer_id=entry.proposer_id,
                pins={label: str(getattr(entry, label, "")) for label in labels},
                current=entry.is_current(now),
            )
            for entry in loaded.registry.proposers
        ),
    )


def _mandate_status(
    path: Path, *, registry_path: Path, registry_loaded: Any | None, now: datetime
) -> tuple[ConditionResult, Any | None]:
    from chronos.api.autonomy_wiring import UnsafeMandateFile, load_persistent_mandate
    from chronos.cli.mandate_check import Severity, review_mandate

    try:
        loaded = load_persistent_mandate(path)
    except UnsafeMandateFile:
        return (
            _result(
                "blocking mandate finding",
                ConditionState.TRIPPED,
                "SHADOW_CAMPAIGN §1",
                "mandate grant is unsafe",
                "run mandate check and repair the grant file",
            ),
            None,
        )
    if loaded is None:
        return (
            _result(
                "blocking mandate finding",
                ConditionState.UNVERIFIED,
                "SHADOW_CAMPAIGN §1",
                "mandate is absent or invalid",
                "run mandate check",
            ),
            None,
        )
    registry = (
        _registry_posture(registry_loaded, path=registry_path, now=now)
        if registry_loaded is not None
        else None
    )
    findings = review_mandate(loaded.mandate, now=now, registry=registry)
    blocking = [finding.code for finding in findings if finding.severity is Severity.BLOCKING]
    if blocking:
        return (
            _result(
                "blocking mandate finding",
                ConditionState.TRIPPED,
                "SHADOW_CAMPAIGN §1",
                "BLOCKING=" + ",".join(sorted(blocking)),
                "run mandate check and resolve every BLOCKING finding",
            ),
            loaded.mandate,
        )
    return (
        _result(
            "blocking mandate finding",
            ConditionState.CLEAR,
            "SHADOW_CAMPAIGN §1",
            "no BLOCKING mandate finding",
            "run mandate check after any grant or posture change",
        ),
        loaded.mandate,
    )


def _read_revocations(database: Path) -> dict[str, bool] | None:
    try:
        with _read_only_database(database) as connection:
            rows = connection.execute(
                "SELECT secret_sha256 FROM autonomy_proposer_revocations"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    return {str(row[0]): True for row in rows}


def _credential_status(
    path: Path, *, database: Path, now: datetime
) -> tuple[ConditionResult, Any | None]:
    from chronos.cli.proposer_commands import _entry_state
    from chronos.supervisor.proposers import UnsafeProposerRegistry, load_proposer_registry

    try:
        loaded = load_proposer_registry(path)
    except UnsafeProposerRegistry:
        return (
            _result(
                "credential expiry/revocation",
                ConditionState.TRIPPED,
                "SHADOW_CAMPAIGN §1",
                "proposer registry grant is unsafe",
                "repair the registry and run proposer check --database-url",
            ),
            None,
        )
    if loaded is None:
        return (
            _result(
                "credential expiry/revocation",
                ConditionState.UNVERIFIED,
                "SHADOW_CAMPAIGN §1",
                "registry is absent or invalid",
                "run proposer check --database-url",
            ),
            None,
        )
    revocations = _read_revocations(database)
    if revocations is None:
        return (
            _result(
                "credential expiry/revocation",
                ConditionState.UNVERIFIED,
                "SHADOW_CAMPAIGN §1",
                "revocation ledger could not be read",
                "run proposer check --database-url against the campaign database",
            ),
            loaded,
        )
    states = Counter(
        _entry_state(entry, now=now, revocations=revocations) for entry in loaded.registry.proposers
    )
    stopped = states["REVOKED"] + states["EXPIRED"]
    if stopped:
        detail = ", ".join(
            f"{name}={states[name]}" for name in ("REVOKED", "EXPIRED") if states[name]
        )
        return (
            _result(
                "credential expiry/revocation",
                ConditionState.TRIPPED,
                "SHADOW_CAMPAIGN §1",
                detail,
                "mint a replacement credential, update the registry, and restart",
            ),
            loaded,
        )
    if not states["CURRENT"]:
        return (
            _result(
                "credential expiry/revocation",
                ConditionState.UNVERIFIED,
                "SHADOW_CAMPAIGN §1",
                "registry has no current credential",
                "run proposer check --database-url",
            ),
            loaded,
        )
    return (
        _result(
            "credential expiry/revocation",
            ConditionState.CLEAR,
            "SHADOW_CAMPAIGN §1",
            f"CURRENT={states['CURRENT']}",
            "re-run after credential rotation or registry edits",
        ),
        loaded,
    )


def _recovery_status(state_dir: Path, database: Path) -> ConditionResult:
    from chronos.orders.recovery_hold import evaluate_recovery_hold, read_restore_pending_token
    from chronos.orders.state_generation import CorruptStateGeneration, StateGenerationMarker

    marker_path = state_dir / "state_generation.json"
    marker_id: str | None = None
    marker_unreadable = False
    try:
        marker = StateGenerationMarker(marker_path).read()
        marker_id = marker.installation_id if marker is not None else None
    except CorruptStateGeneration:
        marker_unreadable = True
    row_present = False
    recorded: str | None = None
    try:
        with _read_only_database(database) as connection:
            row = connection.execute(
                "SELECT installation_id FROM installation_identity WHERE id=1"
            ).fetchone()
        row_present = row is not None
        recorded = str(row[0]) if row is not None and row[0] is not None else None
    except (OSError, sqlite3.Error):
        pass
    if not marker_path.exists() or not row_present or recorded is None:
        missing: list[str] = []
        if not marker_path.exists():
            missing.append("state_generation marker")
        if not row_present:
            missing.append("installation_identity row")
        if row_present and recorded is None:
            missing.append("pending 0012 adoption sentinel")
        return _result(
            "recovery hold",
            ConditionState.UNVERIFIED,
            "ADR-0054 / SHADOW_CAMPAIGN §5",
            "missing " + " and ".join(missing),
            "boot the backend writer once, then re-run campaign status",
        )
    hold = evaluate_recovery_hold(
        marker_installation_id=marker_id,
        marker_unreadable=marker_unreadable,
        recorded_installation_id=recorded,
        restore_pending_token=read_restore_pending_token(state_dir / "recovery_pending.json"),
    )
    if hold is not None:
        return _result(
            "recovery hold",
            ConditionState.TRIPPED,
            "ADR-0054 / SHADOW_CAMPAIGN §5",
            hold.reason.value,
            "follow ADR-0054 recovery acknowledgement with an operator note",
        )
    return _result(
        "recovery hold",
        ConditionState.CLEAR,
        "ADR-0054 / SHADOW_CAMPAIGN §5",
        "installation witnesses agree",
        "preserve the state directory and database together",
    )


def _kill_switch_status(path: Path) -> ConditionResult:
    from chronos.orders.kill_switch import LiveKillSwitch

    if not path.exists():
        return _result(
            "kill switch provenance",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §5",
            "kill-switch file is absent",
            "boot the writer once and inspect live status",
        )
    state = LiveKillSwitch(path).read()
    if state.reason.startswith("kill-switch file unreadable"):
        return _result(
            "kill switch provenance",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §5",
            "kill-switch file is malformed or unreadable",
            "repair the file through the operator kill-switch procedure",
        )
    if state.engaged and state.initiated_by in {"", "system"}:
        return _result(
            "kill switch provenance",
            ConditionState.TRIPPED,
            "SHADOW_CAMPAIGN §5",
            "ENGAGED without an operator attribution",
            "stop and determine why the switch engaged before restarting",
        )
    if not state.initiated_by or (not state.engaged and not state.note):
        return _result(
            "kill switch provenance",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §5",
            "operator record is incomplete",
            "inspect live status and record the operator action",
        )
    return _result(
        "kill switch provenance",
        ConditionState.CLEAR,
        "SHADOW_CAMPAIGN §5",
        "state has a complete operator attribution",
        "retain the operator record",
    )


def _audit_status(path: Path) -> ConditionResult:
    from chronos.auditlog.log import ChainState, verify_chain

    try:
        verification = verify_chain(path)
    except OSError:
        return _result(
            "platform audit chain",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §5",
            "platform audit file is unreadable",
            "preserve the file and run verify-audit-log",
        )
    if verification.state is ChainState.ABSENT:
        # Absence was already UNVERIFIED here, via a separate path.exists() pre-check.
        # It now comes from the returned state, so this caller and the verifier cannot
        # disagree about what "absent" means.
        return _result(
            "platform audit chain",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §5",
            "platform audit file is absent",
            "run verify-audit-log against the campaign audit file",
        )
    return _result(
        "platform audit chain",
        ConditionState.CLEAR if verification.state is ChainState.VALID else ConditionState.TRIPPED,
        "SHADOW_CAMPAIGN §5",
        verification.detail,
        "run verify-audit-log and investigate the first broken record",
    )


def _campaign_chain_status(database: Path) -> ConditionResult:
    from sqlalchemy import create_engine
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import Session

    from chronos.persistence.hash_chain import verify

    try:
        with _read_only_database(database) as connection:
            streams = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT stream FROM hash_chain_records ORDER BY stream"
                )
            )
    except (OSError, sqlite3.Error):
        return _result(
            "campaign audit chain",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §5",
            "campaign chain table is absent or unreadable",
            "preserve the database and follow the journal integrity procedure",
        )
    if not streams:
        return _result(
            "campaign audit chain",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §5",
            "campaign chain has no records",
            "boot the backend writer and complete one observed campaign cycle",
        )

    encoded = quote(str(database.resolve()), safe="/")
    engine = create_engine(f"sqlite+pysqlite:///file:{encoded}?mode=ro&uri=true")
    try:
        with Session(engine) as session:
            results = tuple(verify(session, stream) for stream in streams)
    except (OSError, SQLAlchemyError):
        return _result(
            "campaign audit chain",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §5",
            "campaign chain could not be verified read-only",
            "preserve the database and follow the journal integrity procedure",
        )
    finally:
        engine.dispose()
    broken = next((result for result in results if not result.ok), None)
    if broken is not None:
        return _result(
            "campaign audit chain",
            ConditionState.TRIPPED,
            "SHADOW_CAMPAIGN §5",
            f"a campaign stream failed at record {broken.broken_at}",
            "stop the campaign and follow the journal integrity procedure",
        )
    records = sum(result.records for result in results)
    return _result(
        "campaign audit chain",
        ConditionState.CLEAR,
        "SHADOW_CAMPAIGN §5",
        f"{records} record(s) verified across {len(results)} stream(s)",
        "retain the database and audit evidence together",
    )


def _stored_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("recorded_at is not text")
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _terminal_status(database: Path) -> ConditionResult:
    from chronos.persistence.hash_chain import compute_hash

    try:
        with _read_only_database(database) as connection:
            rows = connection.execute(
                """
                SELECT stream, sequence, recorded_at, payload_json, previous_hash, record_hash
                FROM hash_chain_records
                WHERE stream LIKE 'autonomy.cycles:%'
                ORDER BY stream, sequence
                """
            ).fetchall()
    except (OSError, sqlite3.Error):
        return _result(
            "terminal journal recomputation",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §5",
            "terminal cycle rows or required columns are unavailable",
            "preserve the database and follow the journal integrity procedure",
        )
    if not rows:
        return _result(
            "terminal journal recomputation",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §5",
            "terminal cycle journal has no rows",
            "complete one observed campaign cycle, then re-run status",
        )
    try:
        for stream, sequence, recorded_at, payload_json, previous_hash, record_hash in rows:
            recomputed = compute_hash(
                stream=str(stream),
                sequence=int(sequence),
                recorded_at=_stored_datetime(recorded_at),
                payload_json=str(payload_json),
                previous_hash=str(previous_hash),
            )
            if recomputed != record_hash:
                return _result(
                    "terminal journal recomputation",
                    ConditionState.TRIPPED,
                    "SHADOW_CAMPAIGN §5",
                    f"cycle record {sequence} does not match its digest",
                    "stop the campaign and follow the journal integrity procedure",
                )
    except (OverflowError, TypeError, ValueError):
        return _result(
            "terminal journal recomputation",
            ConditionState.TRIPPED,
            "SHADOW_CAMPAIGN §5",
            "a terminal cycle row cannot be recomputed",
            "stop the campaign and follow the journal integrity procedure",
        )
    return _result(
        "terminal journal recomputation",
        ConditionState.CLEAR,
        "SHADOW_CAMPAIGN §5",
        f"{len(rows)} terminal cycle record(s) match their stored digests",
        "retain the database and audit evidence together",
    )


def _health_status(path: Path, *, now: datetime) -> ConditionResult:
    from chronos.api.routes.health import HealthResponse
    from chronos.operations.health import (
        BackgroundTaskName,
        ObservationState,
        TaskState,
    )

    try:
        raw = path.read_bytes()
        health = HealthResponse.model_validate_json(raw, extra="forbid")
    except (OSError, ValueError):
        return _result(
            "worker liveness",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §3/§5",
            "snapshot is absent or is not an exact /health response",
            "capture curl -fsS http://127.0.0.1:8765/health > health.json",
        )
    age = now - health.assessed_at
    if age < timedelta(0) or age > _HEALTH_SNAPSHOT_MAX_AGE:
        return _result(
            "worker liveness",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §3/§5",
            f"health snapshot age is {int(age.total_seconds())} seconds",
            "capture a fresh /health response and re-run",
        )
    if health.observations.startup_faults:
        faults = ",".join(fault.value for fault in health.observations.startup_faults)
        return _result(
            "worker liveness",
            ConditionState.TRIPPED,
            "SHADOW_CAMPAIGN §3/§5",
            f"startup faults={faults}",
            "stop and resolve every startup fault",
        )
    autonomy = next(
        (task for task in health.observations.tasks if task.name is BackgroundTaskName.AUTONOMY),
        None,
    )
    if autonomy is None:
        return _result(
            "worker liveness",
            ConditionState.UNVERIFIED,
            "SHADOW_CAMPAIGN §3/§5",
            "health response has no autonomy task observation",
            "confirm the writer boot and capture a fresh /health response",
        )
    if (
        autonomy.state is not TaskState.RUNNING
        or autonomy.observation_state is not ObservationState.CURRENT
    ):
        return _result(
            "worker liveness",
            ConditionState.TRIPPED,
            "SHADOW_CAMPAIGN §3/§5",
            f"autonomy task={autonomy.state.value}/{autonomy.observation_state.value}",
            "stop and resolve worker/backend liveness before restarting the campaign",
        )
    return _result(
        "worker liveness",
        ConditionState.CLEAR,
        "SHADOW_CAMPAIGN §3/§5",
        "autonomy task is RUNNING with a CURRENT observation",
        "capture a fresh /health response for each status run",
    )


def _observed_counts(database: Path) -> tuple[int | None, Counter[str] | None]:
    from chronos.supervisor.admission import AdmissionRefusal

    try:
        with _read_only_database(database) as connection:
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(refusals), 0) FROM autonomy_decision_attempts"
            ).fetchone()
            decision_rows = connection.execute(
                """
                SELECT payload_json FROM hash_chain_records
                WHERE stream LIKE 'autonomy.decisions:%' AND kind = 'refused'
                """
            ).fetchall()
    except (OSError, sqlite3.Error):
        return None, None
    if row is None:
        return 0, Counter()
    total_refusals = int(row[1])
    valid_reasons = {reason.value for reason in AdmissionRefusal}
    reasons: Counter[str] = Counter()
    for (payload_json,) in decision_rows:
        try:
            reason = json.loads(str(payload_json)).get("refusal")
        except (AttributeError, TypeError, ValueError):
            continue
        if reason in valid_reasons:
            reasons[str(reason)] += 1
    unattributed = total_refusals - sum(reasons.values())
    if unattributed > 0:
        reasons["UNATTRIBUTED"] = unattributed
    return int(row[0]), reasons


def cmd_campaign_status(args: argparse.Namespace) -> int:
    """Evaluate campaign stop conditions without network access or writes."""

    now: datetime = args.now or datetime.now(tz=UTC)
    if now.tzinfo is None:
        raise ValueError("campaign status --now must be timezone-aware")
    now = now.astimezone(UTC)
    print("Chronos campaign status (read-only; no network or writes)")
    print(f"clock basis: {now.isoformat()}")

    credential, registry_loaded = _credential_status(args.registry, database=args.database, now=now)
    mandate_result, mandate = _mandate_status(
        args.mandate, registry_path=args.registry, registry_loaded=registry_loaded, now=now
    )
    if mandate is None:
        print("campaign day-count: UNVERIFIED")
    else:
        print(f"campaign day-count: {max(0, (now - mandate.effective_from).days)}")
    cycles, refusals = _observed_counts(args.database)
    print(f"cycles observed: {cycles if cycles is not None else 'UNVERIFIED'}")
    print(
        "refusals by reason: "
        + (
            ", ".join(f"{reason}={refusals[reason]}" for reason in sorted(refusals))
            if refusals is not None
            else "UNVERIFIED"
        )
    )

    results = (
        _recovery_status(args.state_dir, args.database),
        mandate_result,
        credential,
        _kill_switch_status(args.kill_switch),
        _audit_status(args.audit_file),
        _campaign_chain_status(args.database),
        _terminal_status(args.database),
        _health_status(args.health_snapshot, now=now),
    )
    for result in results:
        print(
            f"{result.name}: {result.state.value} [{result.section}] "
            f"{result.detail}; repair: {result.repair}"
        )
    blocked = [result for result in results if result.state is not ConditionState.CLEAR]
    if blocked:
        print(f"CAMPAIGN STATUS: UNVERIFIED OR TRIPPED ({len(blocked)} condition(s))")
        return 1
    print("CAMPAIGN STATUS: CLEAR")
    return 0


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--now must be timezone-aware, e.g. 2026-09-05T14:00:00Z")
    return parsed


def add_campaign_status_command(sub: Any) -> None:
    """Register ``campaign status`` on the operator CLI."""

    status = sub.add_parser(
        "status", help="report campaign counters and stop conditions (read-only)"
    )
    status.add_argument("--mandate", type=Path, required=True)
    status.add_argument("--registry", type=Path, required=True)
    status.add_argument("--database", type=Path, required=True)
    status.add_argument("--state-dir", type=Path, required=True)
    status.add_argument("--kill-switch", type=Path, required=True)
    status.add_argument("--audit-file", type=Path, required=True)
    status.add_argument("--health-snapshot", type=Path, required=True)
    status.add_argument(
        "--now",
        type=_parse_instant,
        default=None,
        help="aware ISO-8601 clock basis (default: current UTC)",
    )
    status.set_defaults(func=cmd_campaign_status)
