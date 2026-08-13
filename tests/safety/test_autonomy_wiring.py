"""Assembling autonomy for real, and the invariants that survive assembly (ADR-0017, M7.5).

Every other autonomy test exercises a *piece*: the mandate validates, the cycle
sizes, the runtime ticks. This file exercises the seam that ADR-0017 introduced —
the app-plane wiring that turns "a mandate file on disk plus a running backend"
into a live autonomy runtime with no per-boot ritual.

The properties pinned here are the ones the owner-directed supersession is
allowed to change and the ones it is *not*:

- Auto-activation is the new default: a valid, account-matching mandate file
  activates itself on boot (supersedes ADR-0016 "an env var alone may not
  activate live").
- Revocation still wins over a restart. A mandate the owner stood down does not
  come back because the process bounced — that is the one line the persistence
  of the grant may never cross.
- No grant, a broken grant, or a grant for another account all boot **inert**,
  loudly. The backend still runs (it can still close positions); trading stays
  off until the file is right.
- The order handoff is the full existing pipeline — propose → preview → confirm
  → submit — not a shortcut around it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from chronos.api.autonomy_wiring import (
    ConfirmationRefusedByOrderPlane,
    RiskRefusedByOrderPlane,
    _market_evidence,
    _quote_evidence,
    _reference_price,
    build_autonomy_runtime,
    ensure_activation,
    load_persistent_mandate,
    order_plane_handoff,
)
from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    FamilyPromotion,
    InstrumentScope,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.domain.enums import DataQuality
from chronos.domain.models import MarketQuote, UnderlyingContract
from chronos.orders.submission import SubmissionOutcome, SubmissionRefusalCode
from chronos.persistence.database import Database
from chronos.persistence.schema import AutonomyMandateActivationRow
from chronos.supervisor import alerts, durable
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.handoff import HandoffDisposition
from chronos.supervisor.runtime import AutonomyRuntime
from chronos.utils.identifiers import account_fingerprint

_NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
_ACCOUNT_ID = "DU1234567"
_FINGERPRINT = account_fingerprint(_ACCOUNT_ID)


@pytest.fixture
def database() -> Iterator[Database]:
    instance = Database("sqlite+pysqlite:///:memory:")
    instance.initialize()
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture
def sessions(database: Database) -> sessionmaker[Session]:
    return database.sessions


def _mandate(*, fingerprint: str = _FINGERPRINT, **overrides: Any) -> AutonomyMandate:
    base: dict[str, Any] = {
        "mandate_id": "m-persistent-1",
        "mandate_version": 1,
        "account_fingerprint": fingerprint,
        "mode": AutonomyMode.PAPER_AUTONOMOUS,
        "promotions": (
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.PAPER_AUTONOMOUS
            ),
        ),
        "effective_from": _NOW - timedelta(hours=1),
        "expires_at": _NOW + timedelta(days=1),
        "versions": VersionPins(
            provider="external-worker",
            model_id="ingress",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
        ),
        "scope": InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        "capital": CapitalLimits(
            allocated_capital_usd=Decimal(50_000),
            max_order_notional_usd=Decimal(10_000),
            max_gross_exposure_usd=Decimal(500_000),
            max_net_exposure_usd=Decimal(500_000),
            max_position_notional_usd=Decimal(100_000),
            max_shares_per_order=100,
            min_cash_floor_usd=Decimal(1_000),
            min_buying_power_usd=Decimal(500),
        ),
        "concentration": ConcentrationLimits(max_symbol_exposure_pct=Decimal("0.50")),
        "market_data": MarketDataRequirements(
            max_quote_age_seconds=Decimal(5),
            permitted_data_qualities=(DataQuality.LIVE,),
        ),
        "owner_authorization_ref": "owner-1",
        "authored_at": _NOW,
    }
    base.update(overrides)
    return AutonomyMandate(**base)


def _write_mandate(tmp_path: Path, mandate: AutonomyMandate, *, name: str = "mandate.json") -> Path:
    path = tmp_path / name
    path.write_text(mandate.model_dump_json())
    return path


def _settings(
    tmp_path: Path, *, mandate_file: Path | None, proposers_file: Path | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        autonomy_mandate_file=mandate_file,
        autonomy_proposers_file=proposers_file,
        autonomy_min_interval_seconds=5.0,
        autonomy_idle_interval_seconds=60.0,
        autonomy_alert_file=tmp_path / "alerts.jsonl",
        autonomy_market_timezone="America/New_York",
    )


def _runtime(
    database: Database,
    tmp_path: Path,
    *,
    mandate_file: Path | None = None,
    account_id: str = _ACCOUNT_ID,
    order_management: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        settings=_settings(tmp_path, mandate_file=mandate_file),
        database=database,
        order_management=order_management or SimpleNamespace(account_id=account_id),
    )


def _activation_count(sessions: sessionmaker[Session]) -> int:
    with sessions.begin() as session:
        return int(
            session.scalar(select(func.count()).select_from(AutonomyMandateActivationRow)) or 0
        )


# --------------------------------------------------------- load_persistent_mandate


def test_a_valid_mandate_file_loads_with_a_content_digest(tmp_path: Path) -> None:
    mandate = _mandate()
    path = _write_mandate(tmp_path, mandate)
    loaded = load_persistent_mandate(path)
    assert loaded is not None
    assert loaded.mandate.mandate_id == "m-persistent-1"
    # The digest is over the exact bytes on disk — that is what the activation
    # row cites, so "which text granted this" is answerable forever.
    assert loaded.digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_an_invalid_mandate_file_is_no_mandate(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{ this is not a mandate")
    assert load_persistent_mandate(path) is None


def test_a_missing_mandate_file_is_no_mandate(tmp_path: Path) -> None:
    assert load_persistent_mandate(tmp_path / "does-not-exist.json") is None


def test_editing_the_file_changes_the_digest(tmp_path: Path) -> None:
    """An edited grant must be distinguishable in the audit trail from the old one."""

    first = load_persistent_mandate(_write_mandate(tmp_path, _mandate(), name="a.json"))
    second = load_persistent_mandate(
        _write_mandate(tmp_path, _mandate(mandate_version=2), name="b.json")
    )
    assert first is not None and second is not None
    assert first.digest != second.digest


# ------------------------------------------------------------- ensure_activation


def test_a_fresh_mandate_auto_activates(sessions: sessionmaker[Session]) -> None:
    mandate = _mandate()
    result = ensure_activation(
        SimpleNamespace(database=_db_of(sessions)),
        _loaded(mandate),
        fingerprint=_FINGERPRINT,
        process_generation=7,
        now=_NOW,
    )
    assert result is True
    with sessions.begin() as session:
        activation = durable.load_activation(
            session, account_fingerprint=_FINGERPRINT, mandate=mandate
        )
    assert activation is not None
    assert activation.owner_event_id.startswith("persistent-mandate:")
    assert activation.revoked is False


def test_auto_activation_is_idempotent_across_restarts(sessions: sessionmaker[Session]) -> None:
    """Two boots of the same grant leave one activation, not a growing pile."""

    mandate = _mandate()
    runtime = SimpleNamespace(database=_db_of(sessions))
    for _ in range(3):
        assert ensure_activation(
            runtime,
            _loaded(mandate),
            fingerprint=_FINGERPRINT,
            process_generation=7,
            now=_NOW,
        )
    assert _activation_count(sessions) == 1


def test_a_revoked_mandate_does_not_reactivate_on_restart(
    sessions: sessionmaker[Session],
) -> None:
    """The ADR-0017 line the persistence of the grant may not cross."""

    mandate = _mandate()
    with sessions.begin() as session:
        durable.activate(
            session,
            account_fingerprint=_FINGERPRINT,
            mandate=mandate,
            owner_event_id="owner-original",
            now=_NOW,
            process_generation=7,
        )
    with sessions.begin() as session:
        assert durable.revoke(
            session,
            account_fingerprint=_FINGERPRINT,
            mandate_id=mandate.mandate_id,
            reason="owner stood the system down",
            now=_NOW,
        )

    result = ensure_activation(
        SimpleNamespace(database=_db_of(sessions)),
        _loaded(mandate),
        fingerprint=_FINGERPRINT,
        process_generation=8,  # a new process generation, i.e. a restart
        now=_NOW + timedelta(minutes=1),
    )
    assert result is False
    with sessions.begin() as session:
        raised = alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)
    assert any(alert.kind == "autonomy.revoked_mandate_present" for alert in raised)


# ---------------------------------------------------------- build_autonomy_runtime


def test_no_mandate_file_boots_inert(database: Database, tmp_path: Path) -> None:
    """The one default that stays non-maximal on purpose: no grant, no runtime."""

    runtime = _runtime(database, tmp_path, mandate_file=None)
    assert build_autonomy_runtime(runtime, process_generation=1, is_writer=lambda: True) is None
    assert _activation_count(database.sessions) == 0


def test_an_invalid_mandate_file_boots_inert_with_a_critical_alert(
    database: Database, tmp_path: Path
) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{ not a mandate")
    runtime = _runtime(database, tmp_path, mandate_file=path)

    assert build_autonomy_runtime(runtime, process_generation=1, is_writer=lambda: True) is None
    with database.sessions.begin() as session:
        raised = alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)
    assert any(
        alert.kind == "autonomy.mandate_invalid" and alert.severity is alerts.AlertSeverity.CRITICAL
        for alert in raised
    )


def test_a_mandate_for_another_account_boots_inert(database: Database, tmp_path: Path) -> None:
    """A grant that names a different account is not this account's authority."""

    stranger = _mandate(fingerprint="f" * 64)
    path = _write_mandate(tmp_path, stranger)
    runtime = _runtime(database, tmp_path, mandate_file=path)

    assert build_autonomy_runtime(runtime, process_generation=1, is_writer=lambda: True) is None
    assert _activation_count(database.sessions) == 0
    with database.sessions.begin() as session:
        raised = alerts.unacknowledged(session, account_fingerprint=_FINGERPRINT)
    assert any(alert.kind == "autonomy.mandate_invalid" for alert in raised)


def test_a_matching_mandate_assembles_a_live_runtime(database: Database, tmp_path: Path) -> None:
    path = _write_mandate(tmp_path, _mandate())
    runtime = _runtime(database, tmp_path, mandate_file=path)

    autonomy = build_autonomy_runtime(runtime, process_generation=9, is_writer=lambda: True)
    assert isinstance(autonomy, AutonomyRuntime)
    # Assembly is also activation — the mandate is now in force.
    assert _activation_count(database.sessions) == 1
    with database.sessions.begin() as session:
        activation = durable.load_activation(
            session, account_fingerprint=_FINGERPRINT, mandate=_mandate()
        )
    assert activation is not None
    assert activation.process_generation == 9


def test_a_revoked_persistent_mandate_stays_inert_after_restart(
    database: Database, tmp_path: Path
) -> None:
    """End to end: the file is on disk, but a prior revocation keeps boot inert."""

    mandate = _mandate()
    with database.sessions.begin() as session:
        durable.activate(
            session,
            account_fingerprint=_FINGERPRINT,
            mandate=mandate,
            owner_event_id="owner-original",
            now=_NOW,
            process_generation=1,
        )
    with database.sessions.begin() as session:
        durable.revoke(
            session,
            account_fingerprint=_FINGERPRINT,
            mandate_id=mandate.mandate_id,
            reason="stood down",
            now=_NOW,
        )

    path = _write_mandate(tmp_path, mandate)
    runtime = _runtime(database, tmp_path, mandate_file=path)
    assert build_autonomy_runtime(runtime, process_generation=2, is_writer=lambda: True) is None


# ------------------------------------------------------------- order_plane_handoff


class _RecordingService:
    """Records the pipeline call order the handoff drives it through.

    ``submit`` answers with a real :class:`SubmissionOutcome` (A1). A fake that
    returned some other shape would exercise the classifier's fail-closed
    fallback instead of its translation, and the two must stay distinguishable.
    """

    def __init__(self, *, approved: bool = True) -> None:
        self.calls: list[str] = []
        self._approved = approved
        self.submit_lease: bool | None = None

    def propose(self, intent: object, *, now: datetime) -> object:
        self.calls.append("propose")
        return SimpleNamespace(risk=SimpleNamespace(approved=self._approved, decision_id="risk-1"))

    def preview(self, intent: object, *, now: datetime) -> object:
        self.calls.append("preview")
        return object()

    def confirm(self, intent: object, *, risk_decision_id: str, now: datetime) -> object:
        self.calls.append("confirm")
        return object()

    def submit(self, intent: object, *, writer_lease_held: bool, now: datetime) -> object:
        self.calls.append("submit")
        self.submit_lease = writer_lease_held
        return SubmissionOutcome(
            submitted=True,
            refusal=SubmissionRefusalCode.NOT_REFUSED,
            detail="ok",
        )


def test_the_handoff_walks_the_full_pipeline_in_order() -> None:
    """propose → preview → confirm → submit, the same stack a human proposal walks."""

    service = _RecordingService(approved=True)
    runtime = SimpleNamespace(order_management=service)
    handoff = order_plane_handoff(runtime, is_writer=lambda: True)

    receipt = handoff(object())
    assert service.calls == ["propose", "preview", "confirm", "submit"]
    assert service.submit_lease is True
    # A1: what comes back is the supervisor's own vocabulary, carrying the order
    # plane's outcome unread rather than leaking its type into the cycle.
    assert receipt.disposition is HandoffDisposition.SUBMITTED
    assert receipt.raw.submitted is True


def test_a_risk_refusal_raises_and_never_submits() -> None:
    """A risk veto stops the handoff at propose — nothing downstream fires."""

    service = _RecordingService(approved=False)
    runtime = SimpleNamespace(order_management=service)
    handoff = order_plane_handoff(runtime, is_writer=lambda: True)

    with pytest.raises(RiskRefusedByOrderPlane):
        handoff(object())
    assert service.calls == ["propose"]  # preview/confirm/submit never reached


# ------------------------------------------------------------------- fact helpers


def _quote(**overrides: Any) -> MarketQuote:
    base: dict[str, Any] = {
        "contract": UnderlyingContract(con_id=111, symbol="SPY"),
        "timestamp": _NOW,
        "data_quality": DataQuality.LIVE,
        "bid": Decimal("399.98"),
        "ask": Decimal("400.02"),
    }
    base.update(overrides)
    return MarketQuote(**base)


def test_reference_price_is_the_midpoint_or_zero() -> None:
    assert _reference_price(_quote()) == Decimal(400)
    assert _reference_price(_quote(bid=None)) == Decimal(0)
    assert _reference_price(_quote(bid=Decimal(0))) == Decimal(0)


def test_quote_evidence_is_none_without_both_sides() -> None:
    assert _quote_evidence(_quote()) == QuoteEvidence(bid=Decimal("399.98"), ask=Decimal("400.02"))
    assert _quote_evidence(_quote(ask=None)) is None
    assert _quote_evidence(_quote(bid=None)) is None


def test_market_evidence_clamps_a_future_timestamp_to_zero_age() -> None:
    """A quote stamped in the future must not read as negative age."""

    future = _quote(timestamp=_NOW + timedelta(seconds=30))
    evidence = _market_evidence(future, _NOW)
    assert evidence is not None
    assert evidence.quote_age_seconds == Decimal("0")


# ------------------------------------------------------------- small shared helpers


def _db_of(sessions: sessionmaker[Session]) -> SimpleNamespace:
    return SimpleNamespace(sessions=sessions)


def _loaded(mandate: AutonomyMandate) -> Any:
    from chronos.api.autonomy_wiring import LoadedMandate

    return LoadedMandate(mandate=mandate, digest="d" * 64)


def test_confirmation_refused_is_a_distinct_error_type() -> None:
    """The two order-plane refusals are separate types so callers can tell them apart."""

    assert issubclass(RiskRefusedByOrderPlane, RuntimeError)
    assert issubclass(ConfirmationRefusedByOrderPlane, RuntimeError)
    assert RiskRefusedByOrderPlane is not ConfirmationRefusedByOrderPlane


def test_the_handoff_passes_the_live_writer_state_not_a_literal() -> None:
    """A demoted backend must reach `submit` as a NON-writer (2026-08-05).

    Until this test existed the handoff passed the literal ``True``. That was not
    a bypass — the submission boundary re-checks lease ownership in the database
    inside the CAS-to-transmit window (R-24) — but it meant a process demoted
    mid-session by the lease heartbeat was turned away *late*, after propose,
    preview and confirm had already written state, and with a less specific
    refusal than gate 1's ``READ_ONLY_LEASE``.

    The predicate is read per submission rather than captured, because demotion
    happens while the runtime is alive.
    """

    service = _RecordingService(approved=True)
    runtime = SimpleNamespace(order_management=service)
    writer = {"value": True}
    handoff = order_plane_handoff(runtime, is_writer=lambda: writer["value"])

    handoff(object())
    assert service.submit_lease is True, "a writer must submit as a writer"

    # The heartbeat demotes this process mid-session.
    writer["value"] = False
    handoff(object())
    assert service.submit_lease is False, (
        "after demotion the handoff must pass the live writer state, so the order "
        "plane refuses at gate 1 with READ_ONLY_LEASE rather than at the CAS re-check"
    )
