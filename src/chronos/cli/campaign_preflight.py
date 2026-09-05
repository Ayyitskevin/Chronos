"""Read-only preflight checklist for the autonomy SHADOW campaign."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def cmd_campaign_preflight(args: argparse.Namespace) -> int:
    """Read-only owner checklist for the autonomy SHADOW campaign."""
    import importlib
    import sqlite3
    from datetime import UTC, datetime

    worker_config_mod = importlib.import_module("worker.config")
    mandate_mod = importlib.import_module("chronos.api.autonomy_wiring")
    modes_mod = importlib.import_module("chronos.autonomy")
    broker_mod = importlib.import_module("chronos.broker.demo")
    recovery_mod = importlib.import_module("chronos.orders.recovery_hold")
    generation_mod = importlib.import_module("chronos.orders.state_generation")
    proposers_mod = importlib.import_module("chronos.supervisor.proposers")
    WorkerConfigError, load_config = (
        worker_config_mod.WorkerConfigError,
        worker_config_mod.load_config,
    )
    UnsafeMandateFile, load_persistent_mandate = (
        mandate_mod.UnsafeMandateFile,
        mandate_mod.load_persistent_mandate,
    )
    AutonomyMode, DemoBroker = modes_mod.AutonomyMode, broker_mod.DemoBroker
    evaluate_recovery_hold, read_restore_pending_token = (
        recovery_mod.evaluate_recovery_hold,
        recovery_mod.read_restore_pending_token,
    )
    CorruptStateGeneration, StateGenerationMarker = (
        generation_mod.CorruptStateGeneration,
        generation_mod.StateGenerationMarker,
    )
    UnsafeProposerRegistry, load_proposer_registry = (
        proposers_mod.UnsafeProposerRegistry,
        proposers_mod.load_proposer_registry,
    )

    failures: list[str] = []

    def fail(section: str, check: str, detail: str) -> None:
        failures.append(f"FAIL [SHADOW_CAMPAIGN {section}] {check}: {detail}")

    print("Chronos campaign preflight (read-only; no network or writes)")

    mandate = None
    try:
        loaded = load_persistent_mandate(args.mandate)
    except UnsafeMandateFile:
        loaded = None
        fail("§1", "mandate", "UNSAFE GRANT file; repair with mandate check")
    if loaded is None and not any(" mandate:" in item for item in failures):
        fail("§1", "mandate", "ABSENT or INVALID; repair with python -m chronos.cli mandate check")
    elif loaded is not None:
        mandate = loaded.mandate
        if mandate.mode is not AutonomyMode.SHADOW:
            fail("§1", "mandate", f"mode is {mandate.mode.value}, expected SHADOW")
        elif not (mandate.effective_from <= datetime.now(UTC) < mandate.expires_at):
            fail("§1", "mandate", "effective window is not current")
        else:
            print(f"PASS [§1] SHADOW mandate: {args.mandate}")

    try:
        registry_loaded = load_proposer_registry(args.registry)
    except UnsafeProposerRegistry:
        registry_loaded = None
        fail("§1", "proposer registry", "UNSAFE GRANT file; repair with proposer check")
    if registry_loaded is None and not any("proposer registry:" in item for item in failures):
        fail("§1", "proposer registry", "ABSENT or INVALID; repair with proposer check")
    elif registry_loaded is not None:
        current = [
            entry
            for entry in registry_loaded.registry.proposers
            if entry.enabled and entry.is_current(datetime.now(UTC))
        ]
        if not current:
            fail("§1", "proposer registry", "no enabled, unexpired registration")
        else:
            print(
                f"PASS [§1] proposer registry: {args.registry}, "
                f"{len(current)} current registration(s)"
            )

    if not args.evidence:
        fail("§1/§2", "evidence binding", "disabled; set AUTONOMY_EVIDENCE_BUNDLES=1")
    else:
        print("PASS [§1/§2] evidence binding: enabled")

    try:
        worker = load_config(
            {
                "CHRONOS_WORKER_PROVIDER": args.provider,
                "CHRONOS_WORKER_MODEL": args.model,
                "CHRONOS_WORKER_BACKEND_URL": args.worker_backend_url,
                "CHRONOS_WORKER_SYMBOLS": args.worker_symbols,
                "CHRONOS_WORKER_KINDS": "HOLD",
                "CHRONOS_WORKER_POLICY_FILE": str(args.policy),
                "CHRONOS_WORKER_API_TOKEN": "preflight-placeholder",
            }
        )
    except (WorkerConfigError, ValueError) as error:
        fail("§3", "worker configuration", type(error).__name__)
        worker = None
    if worker is not None:
        if worker.provider != "local" or not args.model:
            fail("§3", "model", "provider must be local with an explicit model tag")
        else:
            print(f"PASS [§3] local model: explicit tag={args.model}")

    contracts = frozenset(DemoBroker._make_underlyings())
    mandate_symbols = frozenset(getattr(mandate.scope, "symbols", ()) if mandate else ())
    backend_symbols = frozenset(
        part.strip().upper() for part in args.backend_symbols.split(",") if part.strip()
    )
    worker_symbols = frozenset(worker.symbols) if worker is not None else frozenset()
    for label, values in (
        ("mandate", mandate_symbols),
        ("backend", backend_symbols),
        ("worker", worker_symbols),
    ):
        unknown = sorted(values - contracts)
        if unknown:
            fail("§1/§3", "symbols", f"{label} contains unknown demo symbols: {','.join(unknown)}")
    if mandate and worker_symbols != mandate_symbols:
        fail("§1/§3", "symbols", "worker symbols do not equal mandate scope")
    if not failures or not any("symbols:" in item for item in failures):
        print(f"PASS [§1/§3] symbols: demo contract set {','.join(sorted(contracts))}")

    if worker is not None:
        expected = f"http://{args.backend_host}:{args.backend_port}"
        parsed = worker.backend_url
        if parsed != expected or parsed.split("://", 1)[-1].split(":", 1)[0] not in {
            "127.0.0.1",
            "localhost",
        }:
            fail("§3", "backend URL", f"{worker.backend_url} does not match {expected}")
        else:
            print(f"PASS [§3] backend URL: {expected} (not probed)")
    unit = args.unit.read_text(encoding="utf-8") if args.unit.exists() else ""
    unit_lines = {line.strip() for line in unit.splitlines()}
    if "UnsetEnvironment=CHRONOS_WORKER_FORWARD" not in unit_lines or any(
        line.startswith("Environment=CHRONOS_WORKER_FORWARD") for line in unit_lines
    ):
        fail(
            "§2/§3",
            "forwarding posture",
            "unit must contain only UnsetEnvironment=CHRONOS_WORKER_FORWARD",
        )
    else:
        print("PASS [§2/§3] forwarding posture: UnsetEnvironment=CHRONOS_WORKER_FORWARD")

    marker = StateGenerationMarker(args.state_dir / "state_generation.json")
    marker_id = None
    marker_present = args.state_dir.joinpath("state_generation.json").exists()
    unreadable = False
    try:
        generation = marker.read()
        marker_id = generation.installation_id if generation else None
    except CorruptStateGeneration:
        unreadable = True
    recorded = None
    identity_row_present = False
    db = args.state_dir / "chronos.db"
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT installation_id FROM installation_identity WHERE id=1"
            ).fetchone()
            identity_row_present = row is not None
            recorded = row[0] if row else None
    except (OSError, sqlite3.Error):
        pass
    state_verified = False
    if not marker_present or not identity_row_present:
        missing = []
        if not marker_present:
            missing.append("state_generation marker")
        if not identity_row_present:
            missing.append("installation_identity row")
        fail(
            "ADR-0054",
            "state identity",
            "UNVERIFIED; missing "
            + " and ".join(missing)
            + "; boot the backend writer once, then re-run preflight",
        )
        hold = None
    elif recorded is None:
        fail(
            "ADR-0054",
            "state identity",
            "UNVERIFIED; pending 0012 adoption sentinel; "
            "boot the backend writer once, then re-run preflight",
        )
        hold = None
    else:
        state_verified = True
        hold = evaluate_recovery_hold(
            marker_installation_id=marker_id,
            marker_unreadable=unreadable,
            recorded_installation_id=recorded,
            restore_pending_token=read_restore_pending_token(
                args.state_dir / "recovery_pending.json"
            ),
        )
    if hold is not None:
        fail("ADR-0054", "state identity", hold.reason.value)
    elif state_verified:
        print("PASS [ADR-0054] state identity: marker/database pair consistent; no recovery hold")
    if failures:
        for item in failures:
            print(item)
        print(f"PREFLIGHT FAILED: {len(failures)} check(s)")
        return 1
    print(
        "PREFLIGHT PASS: all campaign Phase A prerequisites are locally coherent; "
        "no service/model/broker reachability was tested"
    )
    return 0


def add_campaign_preflight_command(sub: Any) -> None:
    """Register ``campaign preflight`` on the operator CLI."""

    preflight = sub.add_parser(
        "preflight", help="check SHADOW campaign prerequisites without network or writes"
    )
    preflight.add_argument("--mandate", type=Path, required=True)
    preflight.add_argument("--registry", type=Path, required=True)
    preflight.add_argument("--state-dir", type=Path, required=True)
    preflight.add_argument("--unit", type=Path, default=Path("docs/ops/chronos-worker.service"))
    preflight.add_argument("--policy", type=Path, required=True)
    preflight.add_argument("--provider", default="local")
    preflight.add_argument("--model", required=True)
    preflight.add_argument("--worker-backend-url", required=True)
    preflight.add_argument("--worker-symbols", required=True)
    preflight.add_argument("--backend-symbols", required=True)
    preflight.add_argument("--backend-host", default="127.0.0.1")
    preflight.add_argument("--backend-port", type=int, default=8765)
    preflight.add_argument("--evidence", action="store_true")
    preflight.set_defaults(func=cmd_campaign_preflight)
