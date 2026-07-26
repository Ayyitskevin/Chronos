"""The terminal's disclosure boundary: what the panels may say, and may not (M8a).

``chronos.terminal.views`` is the only place Chronos turns durable supervisory
state into something an operator's browser will render, which makes it the point
where two failures become possible for the first time:

- **Disclosure.** A read-model is the natural place for a broker account id, a
  raw arm phrase, or an untrusted worker payload to leak into a surface that is
  by design easy to look at. These tests assert structurally — over the model
  fields and over the serialized JSON — that none of that can travel.
- **Fabrication.** A panel that renders ``0`` for a figure it never read is
  worse than one that renders nothing, because the operator cannot tell the
  difference and the plausible value is the reassuring one. "No counter row"
  must read as *no authority established*, never *no losses yet*; an unset
  ceiling under ADR-0017 discretion must read as *no ceiling*, never as a zero
  that would look like the tightest possible limit.

Everything here runs against an in-memory SQLite database. Nothing constructs an
order, imports the order plane, or touches a broker — which is also the property
``test_views_import_no_order_or_broker_module`` asserts about the module itself.
"""

from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    FamilyPromotion,
    InstrumentScope,
    LossLimits,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.domain.enums import DataQuality
from chronos.domain.models import ChronosModel
from chronos.persistence.database import Database
from chronos.persistence.schema import AutonomyOwnerAlertRow
from chronos.supervisor import alerts, durable, proposals
from chronos.supervisor.admission import MarketDataEvidence
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.loop import CycleFacts, CycleOutcome, CycleStage, _record
from chronos.supervisor.sizing import AccountEvidence
from chronos.terminal import views
from chronos.utils.identifiers import account_fingerprint

_NOW = datetime(2026, 7, 26, 15, 30, tzinfo=UTC)
_ACCOUNT_ID = "U7654321"
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


class _FakeTick:
    """A tick schedule with no runtime behind it.

    Structural conformance to ``views.TickSchedule`` is the point: if the
    protocol ever grew a method that needed the real ``AutonomyRuntime``, this
    class would stop satisfying it and mypy would say so.
    """

    def __init__(self, *, stopped: bool, seconds: float) -> None:
        self._stopped = stopped
        self._seconds = seconds

    @property
    def stopped(self) -> bool:
        return self._stopped

    def seconds_until_next_tick(self, now: datetime) -> float:
        del now
        return self._seconds


def _mandate(**overrides: Any) -> AutonomyMandate:
    """A submitting mandate that grants model discretion and sets no ceilings.

    This is the ADR-0017 shape the mandate panel most has to get right: the
    owner set the two floors (which discretion never waives) and deliberately
    left every ceiling unset, meaning *no ceiling* — the exact value a zero
    would misrepresent.
    """

    base: dict[str, Any] = {
        "mandate_id": "m-terminal-1",
        "mandate_version": 3,
        "account_fingerprint": _FINGERPRINT,
        "mode": AutonomyMode.PAPER_AUTONOMOUS,
        "promotions": (
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.PAPER_AUTONOMOUS
            ),
        ),
        "effective_from": _NOW - timedelta(hours=2),
        "expires_at": _NOW + timedelta(days=30),
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
            symbols=("SPY", "IWM"),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        "capital": CapitalLimits(
            model_discretion=True,
            min_cash_floor_usd=Decimal(25),
            min_buying_power_usd=Decimal(10),
        ),
        "market_data": MarketDataRequirements(
            max_quote_age_seconds=Decimal(5),
            permitted_data_qualities=(DataQuality.LIVE,),
        ),
        "owner_authorization_ref": "owner-terminal-1",
        "authored_at": _NOW - timedelta(hours=3),
    }
    base.update(overrides)
    return AutonomyMandate(**base)


def _strict_mandate() -> AutonomyMandate:
    """A non-submitting mandate with no discretion and no ceilings set.

    SHADOW skips the submitting-mandate validators, so this is the one shape
    where every ceiling can honestly be left at its deny-by-default zero.
    """

    return AutonomyMandate(
        mandate_id="m-terminal-shadow",
        mandate_version=1,
        account_fingerprint=_FINGERPRINT,
        mode=AutonomyMode.SHADOW,
        effective_from=_NOW - timedelta(hours=1),
        expires_at=_NOW + timedelta(days=1),
        versions=VersionPins(
            provider="external-worker",
            model_id="ingress",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
        ),
        owner_authorization_ref="owner-terminal-shadow",
        authored_at=_NOW - timedelta(hours=2),
    )


def _limit(model: views.MandateView, name: str) -> views.MandateLimitView:
    for group in (model.ceilings or (), model.floors or ()):
        for item in group:
            if item.name == name:
                return item
    raise AssertionError(f"the mandate panel does not carry a limit named {name!r}")


def _facts(*, account_id: str = _ACCOUNT_ID) -> CycleFacts:
    return CycleFacts(
        account_fingerprint=_FINGERPRINT,
        account_id=account_id,
        now=_NOW,
        process_generation=1,
        evidence_bundle_id="owner-workspace",
        evidence_bundle_digest="0" * 64,
        market_data=MarketDataEvidence(quote_age_seconds=Decimal(1), quality=DataQuality.LIVE),
        account=AccountEvidence(
            net_liquidation_usd=Decimal(110),
            total_cash_usd=Decimal(110),
            buying_power_usd=Decimal(110),
        ),
        quote=QuoteEvidence(bid=Decimal(10), ask=Decimal("10.02")),
        contract=None,
        reference_price=Decimal("10.01"),
    )


# ------------------------------------------------------- unknown is never zero


def test_system_view_reports_every_unknown_as_none(sessions: sessionmaker[Session]) -> None:
    """An unscoped, unwired backend knows nothing and must say so field by field."""

    with sessions.begin() as session:
        view = views.system_view(session, account_fingerprint="", now=_NOW, read_only=True)

    assert view.read_only is True
    assert view.autonomy_configured is False
    assert view.autonomy_stopped is None
    assert view.seconds_until_next_tick is None
    assert view.queue_depth is None
    assert view.alerts_unacknowledged is None
    assert view.alerts_critical is None
    assert view.kill_switch_engaged is None
    assert view.live_armed is None
    assert view.mandate_active is None
    assert view.mandate_id is None
    assert view.account_fingerprint is None
    assert view.generated_at == _NOW.isoformat()


def test_unscoped_backend_never_reports_a_zero_depth(sessions: sessionmaker[Session]) -> None:
    """Counting rows for the empty fingerprint returns zero; zero would be a lie.

    A backend bound to no account has nothing to count, which is a different
    fact from an empty queue — and the difference decides whether an operator
    reads "idle" or "not connected to anything".
    """

    with sessions.begin() as session:
        assert proposals.pending_depth(session, account_fingerprint="") == 0
        assert views.queue_view(session, account_fingerprint="", now=_NOW).pending_depth is None
        assert views.alerts_view(session, account_fingerprint="", now=_NOW).alerts == ()
        assert views.journal_view(session, account_fingerprint="", now=_NOW).stream_present is False


def test_stopped_runtime_is_not_an_absent_one(sessions: sessionmaker[Session]) -> None:
    """A stopped tick has no next tick; the schedule is unknown, not zero."""

    with sessions.begin() as session:
        stopped = views.system_view(
            session,
            account_fingerprint=_FINGERPRINT,
            now=_NOW,
            read_only=False,
            autonomy=_FakeTick(stopped=True, seconds=float("inf")),
        )
        running = views.system_view(
            session,
            account_fingerprint=_FINGERPRINT,
            now=_NOW,
            read_only=False,
            autonomy=_FakeTick(stopped=False, seconds=12.5),
        )

    assert stopped.autonomy_configured is True
    assert stopped.autonomy_stopped is True
    assert stopped.seconds_until_next_tick is None
    assert running.autonomy_configured is True
    assert running.autonomy_stopped is False
    assert running.seconds_until_next_tick == 12.5


def test_system_view_serializes_without_a_non_finite_float(
    sessions: sessionmaker[Session],
) -> None:
    """Infinity is not JSON. The panel must not emit something a client cannot parse."""

    with sessions.begin() as session:
        view = views.system_view(
            session,
            account_fingerprint=_FINGERPRINT,
            now=_NOW,
            read_only=False,
            autonomy=_FakeTick(stopped=True, seconds=float("inf")),
        )
    round_tripped = json.loads(json.dumps(view.model_dump(mode="json"), allow_nan=False))
    assert round_tripped["seconds_until_next_tick"] is None


# ------------------------------------------------------------------- counters


def test_counters_view_states_that_no_row_exists(sessions: sessionmaker[Session]) -> None:
    """No counter row means no figures at all — never a set of zeros.

    ``durable.load_counters`` returns zeroed counters for a missing row, which
    is right for admission (it needs something to compare) and wrong for a
    display. This test pins the divergence deliberately.
    """

    with sessions.begin() as session:
        zeroed = durable.load_counters(session, account_fingerprint=_FINGERPRINT, now=_NOW)
        view = views.counters_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)

    assert zeroed.realized_loss_usd == Decimal(0)
    assert view.counters_recorded is False
    assert view.session_date == _NOW.date().isoformat()
    assert view.realized_loss_usd is None
    assert view.drawdown_usd is None
    assert view.drawdown_pct is None
    assert view.peak_equity_usd is None
    assert view.trough_equity_usd is None
    assert view.orders_submitted is None
    assert view.cancellations is None
    assert view.replacements is None
    assert view.turnover_usd is None


def test_counters_view_reports_a_recorded_session(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as session:
        durable.record_equity(
            session, account_fingerprint=_FINGERPRINT, equity_usd=Decimal(120), now=_NOW
        )
        durable.record_equity(
            session, account_fingerprint=_FINGERPRINT, equity_usd=Decimal(90), now=_NOW
        )
        durable.record_activity(
            session,
            account_fingerprint=_FINGERPRINT,
            now=_NOW,
            orders_submitted=2,
            cancellations=1,
            turnover_usd=Decimal("2000.50"),
            realized_loss_usd=Decimal("30.25"),
        )
        view = views.counters_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)

    assert view.counters_recorded is True
    assert view.realized_loss_usd == "30.25"
    assert view.peak_equity_usd == "120"
    assert view.trough_equity_usd == "90"
    assert view.drawdown_usd == "30"
    assert view.orders_submitted == 2
    assert view.cancellations == 1
    assert view.replacements == 0
    assert view.turnover_usd == "2000.5"


def test_counters_recorded_distinguishes_an_all_zero_session(
    sessions: sessionmaker[Session],
) -> None:
    """A session that recorded activity worth zero is not a session with no row."""

    with sessions.begin() as session:
        durable.record_activity(session, account_fingerprint=_FINGERPRINT, now=_NOW)
        view = views.counters_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)

    assert view.counters_recorded is True
    assert view.realized_loss_usd == "0"
    assert view.orders_submitted == 0


def test_money_is_rendered_exactly_and_without_exponents(
    sessions: sessionmaker[Session],
) -> None:
    """Every rendered figure must parse back to the stored Decimal, unrounded.

    The counter columns are fixed-scale numerics, so a naive ``str()`` emits
    ``0E-8`` for an untouched counter — a zero an operator reads as a code — and
    ``1E+2`` for a round hundred. Both are exact and both are unreadable, so the
    panel renders the shortest fixed-point spelling and this test pins that the
    shortening never changes the value.
    """

    with sessions.begin() as session:
        durable.record_equity(
            session,
            account_fingerprint=_FINGERPRINT,
            equity_usd=Decimal(100),
            now=_NOW,
        )
        durable.record_activity(
            session,
            account_fingerprint=_FINGERPRINT,
            now=_NOW,
            realized_loss_usd=Decimal("0.00000001"),
        )
        view = views.counters_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)
        stored = durable.load_counters(session, account_fingerprint=_FINGERPRINT, now=_NOW)

    rendered = {
        view.realized_loss_usd: stored.realized_loss_usd,
        view.peak_equity_usd: stored.peak_equity_usd,
        view.turnover_usd: stored.turnover_usd,
    }
    for text, value in rendered.items():
        assert text is not None
        assert "E" not in text.upper(), f"{text!r} would render as an exponent"
        assert Decimal(text) == value, f"{text!r} is not the same number as {value!r}"
    assert view.peak_equity_usd == "100"
    assert view.realized_loss_usd == "0.00000001"


def test_counters_view_honours_the_market_session_boundary(
    sessions: sessionmaker[Session],
) -> None:
    """R-34: the session key is the market's calendar day when one is supplied."""

    with sessions.begin() as session:
        view = views.counters_view(
            session,
            account_fingerprint=_FINGERPRINT,
            now=datetime(2026, 7, 27, 1, 0, tzinfo=UTC),
            market_timezone="America/New_York",
        )
    assert view.session_date == "2026-07-26"


# -------------------------------------------------------------------- mandate


def test_mandate_view_never_shows_an_unset_ceiling_as_zero(
    sessions: sessionmaker[Session],
) -> None:
    """The three readings of an unset limit are different, and none of them is 0.

    Under ``model_discretion`` an unset sizing ceiling means affordability is
    the only bound. Rendering it as ``0`` would show the owner the tightest
    conceivable limit at the moment they actually granted the loosest one.
    """

    mandate = _mandate()
    with sessions.begin() as session:
        view = views.mandate_view(
            session, account_fingerprint=_FINGERPRINT, now=_NOW, mandate=mandate
        )

    assert view.model_discretion is True
    notional = _limit(view, "capital.max_order_notional_usd")
    assert notional.value is None
    assert notional.effect == "NO_CEILING"

    cash_floor = _limit(view, "capital.min_cash_floor_usd")
    assert cash_floor.value == "25"
    assert cash_floor.effect == "BINDS"

    # Nothing in the panel may carry a zero standing in for "unset".
    for item in (view.ceilings or ()) + (view.floors or ()):
        if item.effect != "BINDS":
            assert item.value is None, f"{item.name} rendered an unset limit as {item.value!r}"


def test_unset_ceiling_without_discretion_authorizes_nothing(
    sessions: sessionmaker[Session],
) -> None:
    """ADR-0016 deny-by-default: the same zero means the opposite without discretion."""

    with sessions.begin() as session:
        view = views.mandate_view(
            session, account_fingerprint=_FINGERPRINT, now=_NOW, mandate=_strict_mandate()
        )

    assert view.model_discretion is False
    notional = _limit(view, "capital.max_order_notional_usd")
    assert notional.value is None
    assert notional.effect == "AUTHORIZES_NOTHING"


def test_unset_loss_ceiling_reads_as_not_enforced(sessions: sessionmaker[Session]) -> None:
    """A zero loss ceiling is unset, not "no loss tolerated" — and nothing breaches it.

    ``durable.limit_breaches`` is the authority; the panel's ``NOT_ENFORCED`` is
    only honest if that function really does refuse to breach on an unset
    ceiling, so this test asserts both halves together.
    """

    mandate = _mandate()
    ruinous = durable.SessionCounters(
        session_date="2026-07-26",
        realized_loss_usd=Decimal(1_000_000),
        peak_equity_usd=Decimal(110),
        trough_equity_usd=Decimal(0),
        orders_submitted=10_000,
    )
    assert durable.limit_breaches(mandate, ruinous) == ()

    with sessions.begin() as session:
        view = views.mandate_view(
            session, account_fingerprint=_FINGERPRINT, now=_NOW, mandate=mandate
        )
    assert _limit(view, "loss.max_session_loss_usd").effect == "NOT_ENFORCED"
    assert _limit(view, "activity.max_orders_per_session").effect == "NOT_ENFORCED"


def test_set_ceilings_bind_and_carry_their_value(sessions: sessionmaker[Session]) -> None:
    """A ceiling the owner did set still binds under discretion — it is an overlay."""

    mandate = _mandate(
        capital=CapitalLimits(
            model_discretion=True,
            max_order_notional_usd=Decimal("250.75"),
            min_cash_floor_usd=Decimal(25),
            min_buying_power_usd=Decimal(10),
        ),
        loss=LossLimits(max_session_loss_usd=Decimal(50)),
        concentration=ConcentrationLimits(max_symbol_exposure_pct=Decimal("0.25")),
    )
    with sessions.begin() as session:
        view = views.mandate_view(
            session, account_fingerprint=_FINGERPRINT, now=_NOW, mandate=mandate
        )

    notional = _limit(view, "capital.max_order_notional_usd")
    assert (notional.value, notional.effect) == ("250.75", "BINDS")
    loss = _limit(view, "loss.max_session_loss_usd")
    assert (loss.value, loss.effect) == ("50", "BINDS")
    concentration = _limit(view, "concentration.max_symbol_exposure_pct")
    assert (concentration.value, concentration.effect) == ("0.25", "BINDS")


def test_mandate_view_with_no_mandate_claims_nothing(sessions: sessionmaker[Session]) -> None:
    """No mandate is not an empty mandate: the scope collections are unknown, not empty."""

    with sessions.begin() as session:
        view = views.mandate_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)

    assert view.mandate_known is False
    assert view.mandate_id is None
    assert view.active is None
    assert view.revoked is None
    assert view.model_discretion is None
    assert view.symbols is None
    assert view.ceilings is None
    assert view.floors is None
    assert view.promotions is None


def test_mandate_view_tracks_activation_and_revocation(
    sessions: sessionmaker[Session],
) -> None:
    """``active`` follows the activation row, and revocation wins."""

    mandate = _mandate()
    with sessions.begin() as session:
        before = views.mandate_view(
            session, account_fingerprint=_FINGERPRINT, now=_NOW, mandate=mandate
        )
        assert before.active is False
        assert before.revoked is None
        assert before.owner_event_id is None

        durable.activate(
            session,
            account_fingerprint=_FINGERPRINT,
            mandate=mandate,
            owner_event_id="owner-event-9",
            now=_NOW - timedelta(minutes=5),
            process_generation=1,
        )
        activated = views.mandate_view(
            session, account_fingerprint=_FINGERPRINT, now=_NOW, mandate=mandate
        )
        assert activated.active is True
        assert activated.revoked is False
        assert activated.owner_event_id == "owner-event-9"
        assert activated.activated_at == (_NOW - timedelta(minutes=5)).isoformat()
        assert activated.mandate_version == 3
        assert activated.mode == "PAPER_AUTONOMOUS"
        assert activated.symbols == ("SPY", "IWM")

        durable.revoke(
            session,
            account_fingerprint=_FINGERPRINT,
            mandate_id=mandate.mandate_id,
            reason="owner stood the system down",
            now=_NOW,
        )
        revoked = views.mandate_view(
            session, account_fingerprint=_FINGERPRINT, now=_NOW, mandate=mandate
        )
    assert revoked.revoked is True
    assert revoked.active is False


def test_mandate_outside_its_window_is_not_active(sessions: sessionmaker[Session]) -> None:
    """An activated mandate that has expired is displayed, and displayed as inactive."""

    mandate = _mandate()
    with sessions.begin() as session:
        durable.activate(
            session,
            account_fingerprint=_FINGERPRINT,
            mandate=mandate,
            owner_event_id="owner-event-9",
            now=_NOW,
            process_generation=1,
        )
        view = views.mandate_view(
            session,
            account_fingerprint=_FINGERPRINT,
            now=mandate.expires_at + timedelta(seconds=1),
            mandate=mandate,
        )
    assert view.mandate_known is True
    assert view.revoked is False
    assert view.active is False


# -------------------------------------------------------------------- journal


def test_journal_reads_the_stream_the_cycle_actually_writes(
    sessions: sessionmaker[Session],
) -> None:
    """Pins ``views.CYCLE_STREAM`` to the literal ``chronos.supervisor.loop`` writes.

    The stream name is duplicated between the writer and this reader. Rather
    than assert the two strings are equal — which would pass if both were
    wrong — this drives the real recorder and reads the result back through the
    panel, so a rename on either side fails here.
    """

    facts = _facts()
    with sessions.begin() as session:
        _record(
            session,
            facts,
            CycleOutcome(
                stage=CycleStage.ADMISSION,
                refusal="OUT_OF_SCOPE",
                detail="TSLA is not in the mandate's symbols scope",
                decision_id="dec-1",
            ),
        )
        view = views.journal_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)

    assert view.stream_present is True
    assert len(view.entries) == 1
    entry = view.entries[0]
    assert entry.stage == "ADMISSION"
    assert entry.refusal == "OUT_OF_SCOPE"
    assert entry.detail == "TSLA is not in the mandate's symbols scope"
    assert entry.decision_id == "dec-1"
    assert entry.quantity is None
    assert entry.limit_price is None
    assert entry.decoded is True
    assert entry.recorded_at == _NOW.isoformat()


def test_journal_is_newest_first_and_bounded(sessions: sessionmaker[Session]) -> None:
    facts = _facts()
    with sessions.begin() as session:
        for index in range(5):
            _record(
                session,
                facts,
                CycleOutcome(stage=CycleStage.SIZING, decision_id=f"dec-{index}"),
            )
        view = views.journal_view(session, account_fingerprint=_FINGERPRINT, now=_NOW, limit=3)

    assert view.limit == 3
    assert [entry.decision_id for entry in view.entries] == ["dec-4", "dec-3", "dec-2"]
    assert [entry.sequence for entry in view.entries] == [5, 4, 3]


def test_journal_limit_is_clamped_rather_than_trusted(
    sessions: sessionmaker[Session],
) -> None:
    """An unbounded ``limit`` from a query string must not become an unbounded query."""

    with sessions.begin() as session:
        assert (
            views.journal_view(
                session, account_fingerprint=_FINGERPRINT, now=_NOW, limit=10_000
            ).limit
            == views.MAX_JOURNAL_LIMIT
        )
        assert (
            views.journal_view(session, account_fingerprint=_FINGERPRINT, now=_NOW, limit=0).limit
            == 1
        )


def test_empty_journal_is_stated_rather_than_implied(sessions: sessionmaker[Session]) -> None:
    """An empty list is not evidence of calm on a database that never ran a cycle."""

    with sessions.begin() as session:
        view = views.journal_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)
    assert view.entries == ()
    assert view.stream_present is False


def test_journal_detail_is_carried_verbatim_and_bounded_out_loud(
    sessions: sessionmaker[Session],
) -> None:
    """Narrative is text: unchanged, never interpreted, and truncation is announced."""

    markup = "<script>alert(1)</script> & <b>bold</b>"
    facts = _facts()
    with sessions.begin() as session:
        _record(
            session,
            facts,
            CycleOutcome(stage=CycleStage.INGRESS, refusal="MALFORMED", detail=markup),
        )
        _record(
            session,
            facts,
            CycleOutcome(stage=CycleStage.INGRESS, detail="x" * (views.MAX_DETAIL_CHARS + 50)),
        )
        view = views.journal_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)

    long_entry, markup_entry = view.entries
    assert markup_entry.detail == markup
    assert markup_entry.detail_truncated is False
    assert len(long_entry.detail) == views.MAX_DETAIL_CHARS
    assert long_entry.detail_truncated is True


# ---------------------------------------------------------------------- queue


def test_queue_view_never_exposes_a_payload(sessions: sessionmaker[Session]) -> None:
    """The stored payload is untrusted worker input and never reaches the browser."""

    payload = json.dumps({"symbol": "SPY", "secret_marker": "PAYLOAD-MUST-NOT-APPEAR"})
    with sessions.begin() as session:
        proposals.enqueue(session, account_fingerprint=_FINGERPRINT, payload=payload, now=_NOW)
        view = views.queue_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)

    assert view.pending_depth == 1
    assert len(view.recent) == 1
    assert view.recent[0].status == proposals.STATUS_PENDING
    assert view.recent[0].queued_at == _NOW.isoformat()
    assert "payload" not in views.QueueEntryView.model_fields
    assert "PAYLOAD-MUST-NOT-APPEAR" not in view.model_dump_json()


def test_queue_view_reports_the_cycle_outcome_of_recent_arrivals(
    sessions: sessionmaker[Session],
) -> None:
    with sessions.begin() as session:
        outcome = proposals.enqueue(
            session, account_fingerprint=_FINGERPRINT, payload="{}", now=_NOW
        )
        proposals.mark_processed(
            session,
            queue_id=outcome.queue_id,
            stage="ADMISSION",
            refusal="OUT_OF_SCOPE",
            now=_NOW,
        )
        view = views.queue_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)

    assert view.pending_depth == 0
    assert view.recent[0].status == proposals.STATUS_PROCESSED
    assert view.recent[0].cycle_stage == "ADMISSION"
    assert view.recent[0].refusal == "OUT_OF_SCOPE"


def test_queue_view_is_newest_first_and_clamped(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as session:
        for index in range(4):
            proposals.enqueue(
                session,
                account_fingerprint=_FINGERPRINT,
                payload=f'{{"n":{index}}}',
                now=_NOW + timedelta(seconds=index),
            )
        view = views.queue_view(session, account_fingerprint=_FINGERPRINT, now=_NOW, limit=2)
        clamped = views.queue_view(
            session, account_fingerprint=_FINGERPRINT, now=_NOW, limit=10_000
        )

    assert view.pending_depth == 4
    assert [entry.queued_at for entry in view.recent] == [
        (_NOW + timedelta(seconds=3)).isoformat(),
        (_NOW + timedelta(seconds=2)).isoformat(),
    ]
    assert clamped.limit == views.MAX_QUEUE_LIMIT


# --------------------------------------------------------------------- alerts


def test_alert_delivery_is_reported_from_delivered_at(
    sessions: sessionmaker[Session],
) -> None:
    """``GET /autonomy/alerts`` hardcodes ``delivered=False``. This must not.

    R-32's whole point is that an alert nobody was told about is not an alert,
    so a panel that cannot distinguish delivered from undelivered has erased the
    only thing the field is for.
    """

    with sessions.begin() as session:
        told = alerts.raise_alert(
            session,
            account_fingerprint=_FINGERPRINT,
            severity=alerts.AlertSeverity.WARNING,
            kind="runtime.tick_failed",
            summary="an autonomy tick failed",
            now=_NOW,
        )
        alerts.raise_alert(
            session,
            account_fingerprint=_FINGERPRINT,
            severity=alerts.AlertSeverity.CRITICAL,
            kind="degraded.market_data",
            summary="the market-data feed is stale",
            now=_NOW,
        )
        told.delivered_at = _NOW
        session.flush()
        view = views.alerts_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)

    by_kind = {entry.kind: entry for entry in view.alerts}
    assert by_kind["runtime.tick_failed"].delivered is True
    assert by_kind["degraded.market_data"].delivered is False
    assert all(entry.acknowledged is False for entry in view.alerts)


def test_alerts_are_ordered_most_severe_first(sessions: sessionmaker[Session]) -> None:
    """Severities are stored as strings; alphabetical order would put CRITICAL first
    by luck and WARNING before INFO by accident. The rank is the authority."""

    with sessions.begin() as session:
        for kind, severity in (
            ("a.info", alerts.AlertSeverity.INFO),
            ("b.warning", alerts.AlertSeverity.WARNING),
            ("c.critical", alerts.AlertSeverity.CRITICAL),
        ):
            alerts.raise_alert(
                session,
                account_fingerprint=_FINGERPRINT,
                severity=severity,
                kind=kind,
                summary=kind,
                now=_NOW,
            )
        view = views.alerts_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)

    assert [entry.severity for entry in view.alerts] == ["CRITICAL", "WARNING", "INFO"]


def test_acknowledged_alerts_leave_the_pending_panel(
    sessions: sessionmaker[Session],
) -> None:
    with sessions.begin() as session:
        raised = alerts.raise_alert(
            session,
            account_fingerprint=_FINGERPRINT,
            severity=alerts.AlertSeverity.WARNING,
            kind="runtime.tick_failed",
            summary="an autonomy tick failed",
            now=_NOW,
        )
        session.flush()
        alerts.acknowledge(
            session,
            account_fingerprint=_FINGERPRINT,
            alert_id=raised.id,
            note="restarted the worker",
            now=_NOW,
        )
        view = views.alerts_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)
        system = views.system_view(
            session, account_fingerprint=_FINGERPRINT, now=_NOW, read_only=False
        )

    assert view.alerts == ()
    assert system.alerts_unacknowledged == 0
    assert system.alerts_critical == 0


def test_system_view_counts_pending_alerts_and_queue_depth(
    sessions: sessionmaker[Session],
) -> None:
    mandate = _mandate()
    with sessions.begin() as session:
        alerts.raise_alert(
            session,
            account_fingerprint=_FINGERPRINT,
            severity=alerts.AlertSeverity.CRITICAL,
            kind="degraded.market_data",
            summary="the market-data feed is stale",
            now=_NOW,
        )
        alerts.raise_alert(
            session,
            account_fingerprint=_FINGERPRINT,
            severity=alerts.AlertSeverity.INFO,
            kind="runtime.note",
            summary="a note",
            now=_NOW,
        )
        proposals.enqueue(session, account_fingerprint=_FINGERPRINT, payload="{}", now=_NOW)
        durable.activate(
            session,
            account_fingerprint=_FINGERPRINT,
            mandate=mandate,
            owner_event_id="owner-event-9",
            now=_NOW,
            process_generation=1,
        )
        view = views.system_view(
            session,
            account_fingerprint=_FINGERPRINT,
            now=_NOW,
            read_only=False,
            mandate=mandate,
            kill_switch_engaged=False,
            live_armed=True,
        )

    assert view.alerts_unacknowledged == 2
    assert view.alerts_critical == 1
    assert view.queue_depth == 1
    assert view.mandate_active is True
    assert view.mandate_id == "m-terminal-1"
    assert view.kill_switch_engaged is False
    assert view.live_armed is True
    assert view.account_fingerprint == _FINGERPRINT


# ------------------------------------------------------- the disclosure bound


def _view_models() -> list[type[ChronosModel]]:
    return [
        member
        for _, member in inspect.getmembers(views, inspect.isclass)
        if issubclass(member, ChronosModel) and member is not ChronosModel
    ]


def test_no_view_model_declares_an_account_or_secret_field() -> None:
    """Structural: the disclosure bound is a property of the field list itself.

    Asserted over the declared fields rather than over one serialized response,
    because a field that is ``None`` in every test fixture would still be a
    field a future assembler could fill.
    """

    forbidden = (
        "account_id",
        "accountid",
        "account_number",
        "credential",
        "password",
        "passphrase",
        "phrase",
        "secret",
        "token",
        "api_key",
        "otp",
        "2fa",
    )
    models = _view_models()
    assert models, "expected the terminal views module to declare response models"
    for model in models:
        for name in model.model_fields:
            if name == "account_fingerprint":
                continue
            lowered = name.lower()
            for needle in forbidden:
                assert needle not in lowered, (
                    f"{model.__name__}.{name} names a secret or a full account identifier"
                )


def test_no_assembled_view_carries_the_raw_account_id(
    sessions: sessionmaker[Session],
) -> None:
    """Value-level: the raw id is present in the state these panels read from.

    ``CycleFacts`` carries the real broker account id and the proposal queue
    holds whatever a worker sent, so this seeds both and then asserts the id
    survives in neither serialized panel.
    """

    mandate = _mandate()
    with sessions.begin() as session:
        _record(
            session,
            _facts(),
            CycleOutcome(stage=CycleStage.HANDOFF, decision_id="dec-1"),
        )
        proposals.enqueue(
            session,
            account_fingerprint=_FINGERPRINT,
            payload=json.dumps({"account": _ACCOUNT_ID}),
            now=_NOW,
        )
        alerts.raise_alert(
            session,
            account_fingerprint=_FINGERPRINT,
            severity=alerts.AlertSeverity.WARNING,
            kind="runtime.tick_failed",
            summary="an autonomy tick failed",
            now=_NOW,
        )
        durable.record_activity(
            session, account_fingerprint=_FINGERPRINT, now=_NOW, orders_submitted=1
        )
        rendered = [
            views.system_view(
                session,
                account_fingerprint=_FINGERPRINT,
                now=_NOW,
                read_only=False,
                mandate=mandate,
            ).model_dump_json(),
            views.mandate_view(
                session, account_fingerprint=_FINGERPRINT, now=_NOW, mandate=mandate
            ).model_dump_json(),
            views.journal_view(
                session, account_fingerprint=_FINGERPRINT, now=_NOW
            ).model_dump_json(),
            views.counters_view(
                session, account_fingerprint=_FINGERPRINT, now=_NOW
            ).model_dump_json(),
            views.queue_view(session, account_fingerprint=_FINGERPRINT, now=_NOW).model_dump_json(),
            views.alerts_view(
                session, account_fingerprint=_FINGERPRINT, now=_NOW
            ).model_dump_json(),
        ]

    for document in rendered:
        assert _ACCOUNT_ID not in document
    assert _FINGERPRINT in rendered[0]


def test_view_models_are_frozen_and_forbid_unknown_fields() -> None:
    """Inherited from ``ChronosModel``, asserted here because the panels rely on it."""

    for model in _view_models():
        assert model.model_config.get("frozen") is True
        assert model.model_config.get("extra") == "forbid"


def test_views_import_no_order_or_broker_module() -> None:
    """The read-models may not name the execution plane (ADR-0018 §4).

    An AST check rather than a ``sys.modules`` probe, and the difference is
    worth stating: importing ``chronos.supervisor`` runs its package ``__init__``,
    which re-exports ``run_cycle`` and therefore pulls ``chronos.orders.intent``
    in transitively. That transitive edge is not this module's to remove. What
    *is* this module's to guarantee is that it never reaches for the order
    plane, the broker, or the execution engine by name — so the kill switch and
    the arm state arrive as booleans and the tick as a protocol.
    """

    forbidden = ("chronos.orders", "chronos.broker", "chronos.execution", "fastapi")
    source = Path(views.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for name in imported:
        for prefix in forbidden:
            assert not (name == prefix or name.startswith(prefix + ".")), (
                f"chronos.terminal.views imports {name!r}, which it must never reach"
            )


def test_assemblers_only_read(sessions: sessionmaker[Session]) -> None:
    """A panel that wrote would be a second place authority is decided.

    Assembling every view against a database with an alert in it must leave the
    row count exactly as it was — no acknowledgement, no delivery stamp, no
    counter increment.
    """

    mandate = _mandate()
    with sessions.begin() as session:
        alerts.raise_alert(
            session,
            account_fingerprint=_FINGERPRINT,
            severity=alerts.AlertSeverity.CRITICAL,
            kind="degraded.market_data",
            summary="the market-data feed is stale",
            now=_NOW,
        )
        proposals.enqueue(session, account_fingerprint=_FINGERPRINT, payload="{}", now=_NOW)

    with sessions.begin() as session:
        views.system_view(
            session,
            account_fingerprint=_FINGERPRINT,
            now=_NOW,
            read_only=False,
            mandate=mandate,
        )
        views.mandate_view(session, account_fingerprint=_FINGERPRINT, now=_NOW, mandate=mandate)
        views.journal_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)
        views.counters_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)
        views.queue_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)
        views.alerts_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)

    with sessions.begin() as session:
        row = session.scalar(select(AutonomyOwnerAlertRow))
        assert row is not None
        assert row.acknowledged_at is None
        assert row.delivered_at is None
        assert row.occurrences == 1
        assert proposals.pending_depth(session, account_fingerprint=_FINGERPRINT) == 1
        counters = views.counters_view(session, account_fingerprint=_FINGERPRINT, now=_NOW)
        assert counters.counters_recorded is False
