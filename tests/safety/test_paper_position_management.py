"""Executable safety contract for the default-off QQQ PAPER position lifecycle."""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from chronos.autonomy import DecisionKind
from chronos.domain.enums import DataQuality
from chronos.persistence import hash_chain
from chronos.persistence.database import Database
from chronos.persistence.schema import HashChainRow
from chronos.supervisor.position_management import (
    QQQ_FIVE_TOOL_PAPER_POLICY,
    QQQ_FIVE_TOOL_PAPER_POLICY_SHA256,
    DirectiveOutcome,
    DirectiveResolution,
    EvaluationRefusal,
    ManagedLegId,
    ManagementReason,
    PositionManagementError,
    PositionObservation,
    build_qqq_five_tool_paper_plan,
    evaluate_position,
    policy_sha256,
    record_directive_resolution,
    register_position,
    rehydrate_position,
)
from chronos.supervisor.queue import economic_fingerprint

_NOW = datetime(2026, 8, 25, 15, 30, tzinfo=UTC)
_FINGERPRINT = "a" * 64
_POSITION = "CHR-POS-" + "1" * 32
_OPENING_ORDER = "CHR-ORD-" + "2" * 32
_EVIDENCE = "b" * 64


@pytest.fixture
def session() -> Iterator[Session]:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        with database.sessions.begin() as db_session:
            yield db_session
    finally:
        database.dispose()


def _plan(**overrides: object):
    values: dict[str, object] = {
        "position_id": _POSITION,
        "opening_order_ref": _OPENING_ORDER,
        "account_fingerprint": _FINGERPRINT,
        "entry_fill_ids": ("exec-open-1",),
        "opening_fill_evidence_digest": "e" * 64,
        "entry_risk_evidence_digest": "f" * 64,
        "opened_at": _NOW - timedelta(hours=1),
        "quantity": Decimal(9),
        "entry_price": Decimal(100),
        "initial_stop_price": Decimal(99),
        "signal_time_risk_distance_usd": Decimal(1),
        "strategy_nav_usd": Decimal(3000),
        "unit_exposure_cvar_loss_fraction": Decimal("0.05"),
    }
    values.update(overrides)
    return build_qqq_five_tool_paper_plan(**values)


def _observation(sequence: int = 1, **overrides: object) -> PositionObservation:
    values: dict[str, object] = {
        "observation_id": f"obs-{sequence}",
        "account_fingerprint": _FINGERPRINT,
        "as_of": _NOW,
        "evidence_digest": _EVIDENCE,
        "data_quality": DataQuality.LIVE,
        "last_price": Decimal("100.50"),
        "marked_strategy_nav_usd": Decimal(3000),
        "broker_position_quantity": Decimal(9),
        "reconciliation_generation": 7,
        "reconciliation_session_id": "recon-7",
    }
    values.update(overrides)
    return PositionObservation(**values)


def _register(session: Session, **plan_overrides: object):
    plan = _plan(**plan_overrides)
    state = register_position(
        session,
        plan=plan,
        recorded_at=_NOW,
    )
    return plan, state


def _resolve(
    session: Session,
    directive_ref: str,
    outcome: DirectiveOutcome,
    *,
    quantity: Decimal = Decimal(0),
    price: Decimal | None = None,
    execution_id: str | None = None,
    occurred_at: datetime | None = None,
):
    return record_directive_resolution(
        session,
        position_id=_POSITION,
        account_fingerprint=_FINGERPRINT,
        resolution=DirectiveResolution(
            directive_ref=directive_ref,
            outcome=outcome,
            occurred_at=occurred_at or _NOW + timedelta(seconds=1),
            evidence_digest="c" * 64,
            execution_id=execution_id,
            fill_quantity=quantity,
            fill_price=price,
        ),
    )


def test_policy_is_the_exact_selected_confluence_stack() -> None:
    policy = QQQ_FIVE_TOOL_PAPER_POLICY
    assert policy.symbol == "QQQ"
    assert policy.long_only is True
    assert policy.native_stop_risk_fraction == Decimal("0.01")
    assert policy.native_stop_risk_usd_max == 30
    assert policy.cvar_risk_fraction == Decimal("0.015")
    assert policy.cvar_risk_usd_max == 45
    assert (policy.target_1_r, policy.target_2_r) == (1, 2)
    assert policy.break_even_after_target_1 is True
    assert (policy.chandelier_lookback, policy.chandelier_atr_multiple) == (22, 3)
    assert policy.opposite_regime_exit is True
    # Source-default asymmetry: dedicated long v2 is off, so this is off.
    assert policy.long_avwap_exit is False
    assert policy.neutral_regime_exit is False
    assert policy.sma_exit is False
    assert policy.time_exit is False
    assert policy.session_loss_fraction == Decimal("0.02")
    assert policy.session_loss_usd_max == 60
    assert policy.drawdown_fraction_max == Decimal("0.10")
    assert policy.permitted_data_qualities == (DataQuality.LIVE,)
    assert policy_sha256() == QQQ_FIVE_TOOL_PAPER_POLICY_SHA256
    assert QQQ_FIVE_TOOL_PAPER_POLICY_SHA256 == (
        "7a5b29eb8055b0b4cf0f80476cca200234cfe96afd5327101da7e76ac09ec188"
    )


def test_plan_uses_actual_fills_exact_risk_and_source_split_geometry() -> None:
    plan = _plan()
    assert plan.native_stop_risk_usd == 9
    assert plan.cvar_projected_loss_usd == 45
    assert plan.applicable_capital_base == 3000
    assert [(leg.leg_id, leg.quantity, leg.target_price) for leg in plan.legs] == [
        (ManagedLegId.TARGET_1, Decimal(3), Decimal(101)),
        (ManagedLegId.TARGET_2, Decimal(3), Decimal(102)),
        (ManagedLegId.RUNNER, Decimal(3), None),
    ]

    two_share = _plan(position_id="CHR-POS-" + "3" * 32, quantity=Decimal(2))
    assert [(leg.leg_id, leg.quantity, leg.target_price) for leg in two_share.legs] == [
        (ManagedLegId.TARGET_2, Decimal(2), Decimal(102))
    ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"initial_stop_price": Decimal(100)}, "below a long entry"),
        ({"quantity": Decimal("1.5")}, "whole shares"),
        (
            {"unit_exposure_cvar_loss_fraction": Decimal("1.01")},
            "may not exceed total exposure",
        ),
    ],
)
def test_plan_refuses_invalid_geometry_or_cvar_observations(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _plan(**overrides)


def test_plan_refuses_noncanonical_leg_order() -> None:
    plan = _plan()
    payload = plan.model_dump()
    payload["legs"] = tuple(reversed(plan.legs))
    with pytest.raises(ValueError, match="canonical T1, T2, RUNNER order"):
        type(plan).model_validate(payload)


def test_trailing_high_must_cover_the_current_price() -> None:
    with pytest.raises(ValueError, match="at least last_price"):
        _observation(
            last_price=Decimal("100.50"),
            highest_high_22=Decimal("100.49"),
            atr14=Decimal("0.20"),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": Decimal(31)},
        {"unit_exposure_cvar_loss_fraction": Decimal("0.06")},
        {
            "entry_price": Decimal(400),
            "initial_stop_price": Decimal("399.80"),
            "signal_time_risk_distance_usd": Decimal("0.20"),
            "unit_exposure_cvar_loss_fraction": Decimal("0.01"),
        },
    ],
)
def test_actual_over_limit_fill_is_registered_only_to_flatten(
    session: Session,
    overrides: dict[str, object],
) -> None:
    plan, state = _register(session, **overrides)
    assert plan.risk_envelope_breached is True
    assert state.flatten_latched_reason is ManagementReason.ENTRY_RISK_BREACH
    result = evaluate_position(
        session,
        position_id=plan.position_id,
        observation=_observation(broker_position_quantity=plan.quantity),
        evaluated_at=_NOW,
    )
    assert result.directive is not None
    assert result.directive.reason is ManagementReason.ENTRY_RISK_BREACH
    assert result.directive.quantity == plan.quantity
    assert result.directive.closes_position is True


def test_no_trigger_observation_is_durable_and_semantically_replayed(session: Session) -> None:
    _register(session)
    result = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(),
        evaluated_at=_NOW,
    )
    assert result.refusal is None
    assert result.directive is None
    assert len(tuple(session.scalars(select(HashChainRow)))) == 2
    replayed = rehydrate_position(
        session,
        account_fingerprint=_FINGERPRINT,
        position_id=_POSITION,
    )
    assert replayed.seen_observation_ids == frozenset({"obs-1"})


def test_registration_is_durable_restart_safe_and_idempotent(session: Session) -> None:
    plan, initial = _register(session)
    assert initial.remaining_quantity == 9
    assert initial.leg_stop_price == 99
    assert initial.runner_stop_price == 99

    replayed = rehydrate_position(
        session,
        account_fingerprint=_FINGERPRINT,
        position_id=_POSITION,
    )
    assert replayed == initial
    duplicate = register_position(
        session,
        plan=plan,
        recorded_at=_NOW,
    )
    assert duplicate == initial
    rows = tuple(session.scalars(select(HashChainRow)))
    assert len(rows) == 1


def test_fresh_no_trigger_observation_is_recorded_once(session: Session) -> None:
    _register(session)
    observation = _observation()
    first = evaluate_position(
        session,
        position_id=_POSITION,
        observation=observation,
        evaluated_at=_NOW,
    )
    assert first.refusal is None
    assert first.directive is None
    assert first.detail == "no management trigger"

    second = evaluate_position(
        session,
        position_id=_POSITION,
        observation=observation,
        evaluated_at=_NOW,
    )
    assert second.refusal is EvaluationRefusal.OBSERVATION_REPLAY
    assert len(tuple(session.scalars(select(HashChainRow)))) == 2


def test_backdated_events_and_out_of_order_observations_refuse_before_action(
    session: Session,
) -> None:
    _register(session)
    with pytest.raises(PositionManagementError, match="predates the durable position state"):
        evaluate_position(
            session,
            position_id=_POSITION,
            observation=_observation(as_of=_NOW - timedelta(seconds=1)),
            evaluated_at=_NOW - timedelta(seconds=1),
        )
    assert len(tuple(session.scalars(select(HashChainRow)))) == 1

    evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(),
        evaluated_at=_NOW,
    )
    out_of_order = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(2, as_of=_NOW - timedelta(milliseconds=1)),
        evaluated_at=_NOW + timedelta(seconds=1),
    )
    assert out_of_order.refusal is EvaluationRefusal.TEMPORAL_ORDER
    assert out_of_order.directive is None


@pytest.mark.parametrize(
    ("observation", "evaluated_at", "refusal"),
    [
        (
            _observation(as_of=_NOW - timedelta(seconds=6)),
            _NOW,
            EvaluationRefusal.STALE_OBSERVATION,
        ),
        (
            _observation(data_quality=DataQuality.DELAYED),
            _NOW,
            EvaluationRefusal.DATA_QUALITY,
        ),
        (
            _observation(broker_position_quantity=Decimal(8)),
            _NOW,
            EvaluationRefusal.RECONCILIATION_MISMATCH,
        ),
        (
            _observation(account_fingerprint="d" * 64),
            _NOW,
            EvaluationRefusal.RECONCILIATION_MISMATCH,
        ),
    ],
)
def test_stale_low_quality_or_unreconciled_observation_cannot_act(
    session: Session,
    observation: PositionObservation,
    evaluated_at: datetime,
    refusal: EvaluationRefusal,
) -> None:
    _register(session)
    if observation.account_fingerprint != _FINGERPRINT:
        # Stream lookup itself is account-scoped and therefore fails before an
        # observation can be tested against another account's position.
        with pytest.raises(PositionManagementError, match="not registered"):
            evaluate_position(
                session,
                position_id=_POSITION,
                observation=observation,
                evaluated_at=evaluated_at,
            )
        return
    result = evaluate_position(
        session,
        position_id=_POSITION,
        observation=observation,
        evaluated_at=evaluated_at,
    )
    assert result.refusal is refusal
    assert result.directive is None


def test_t1_requires_actual_complete_fill_before_breakeven(session: Session) -> None:
    _register(session)
    first = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(last_price=Decimal(101)),
        evaluated_at=_NOW,
    )
    directive = first.directive
    assert directive is not None
    assert directive.reason is ManagementReason.TARGET_1
    assert directive.leg_id is ManagedLegId.TARGET_1
    assert directive.quantity == 3
    assert directive.proposal.kind is DecisionKind.REDUCE
    assert directive.execution_authority == "none"
    assert directive.required_path == "existing_supervisor_and_order_pipeline"

    partial = _resolve(
        session,
        directive.directive_ref,
        DirectiveOutcome.PARTIALLY_FILLED_REMAINDER_CANCELLED,
        quantity=Decimal(1),
        price=Decimal(101),
        execution_id="exec-t1-partial",
    )
    assert partial.target_1_filled is False
    assert partial.leg_stop_price == 99
    assert partial.runner_stop_price == 99
    assert partial.remaining_for(ManagedLegId.TARGET_1) == 2

    second = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(
            2,
            as_of=_NOW + timedelta(seconds=2),
            last_price=Decimal(101),
            broker_position_quantity=Decimal(8),
        ),
        evaluated_at=_NOW + timedelta(seconds=2),
    )
    second_directive = second.directive
    assert second_directive is not None and second_directive.quantity == 2
    filled = record_directive_resolution(
        session,
        position_id=_POSITION,
        account_fingerprint=_FINGERPRINT,
        resolution=DirectiveResolution(
            directive_ref=second_directive.directive_ref,
            outcome=DirectiveOutcome.FILLED,
            occurred_at=_NOW + timedelta(seconds=3),
            evidence_digest="c" * 64,
            execution_id="exec-t1-rest",
            fill_quantity=Decimal(2),
            fill_price=Decimal(101),
        ),
    )
    assert filled.target_1_filled is True
    assert filled.leg_stop_price == 100
    assert filled.runner_stop_price == 100
    assert filled.remaining_quantity == 6


def test_breakeven_and_chandelier_only_tighten_the_stop(session: Session) -> None:
    _register(session, quantity=Decimal(2))
    trailed = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(
            last_price=Decimal("101.50"),
            broker_position_quantity=Decimal(2),
            highest_high_22=Decimal("101.50"),
            atr14=Decimal("0.20"),
        ),
        evaluated_at=_NOW,
    )
    assert trailed.directive is None
    assert trailed.effective_leg_stop_price == 99
    assert trailed.effective_runner_stop_price == Decimal("100.90")
    replayed = rehydrate_position(
        session,
        account_fingerprint=_FINGERPRINT,
        position_id=_POSITION,
    )
    assert replayed.leg_stop_price == 99
    assert replayed.runner_stop_price == Decimal("100.90")

    lower_candidate = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(
            2,
            as_of=_NOW + timedelta(seconds=1),
            last_price=Decimal("101.50"),
            broker_position_quantity=Decimal(2),
            highest_high_22=Decimal("101.50"),
            atr14=Decimal(1),
        ),
        evaluated_at=_NOW + timedelta(seconds=1),
    )
    assert lower_candidate.effective_runner_stop_price == Decimal("100.90")


def test_chandelier_stop_reduces_only_the_runner_in_a_split_position(
    session: Session,
) -> None:
    _register(session)
    target_1 = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(
            last_price=Decimal("101.50"),
            highest_high_22=Decimal("101.50"),
            atr14=Decimal("0.20"),
        ),
        evaluated_at=_NOW,
    ).directive
    assert target_1 is not None and target_1.reason is ManagementReason.TARGET_1
    after_target_1 = _resolve(
        session,
        target_1.directive_ref,
        DirectiveOutcome.FILLED,
        quantity=Decimal(3),
        price=Decimal(101),
        execution_id="exec-t1",
    )
    assert after_target_1.leg_stop_price == 100
    assert after_target_1.runner_stop_price == Decimal("100.90")

    runner_exit = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(
            2,
            as_of=_NOW + timedelta(seconds=2),
            last_price=Decimal("100.50"),
            broker_position_quantity=Decimal(6),
        ),
        evaluated_at=_NOW + timedelta(seconds=2),
    ).directive
    assert runner_exit is not None
    assert runner_exit.reason is ManagementReason.TRAILING_STOP
    assert runner_exit.leg_id is ManagedLegId.RUNNER
    assert runner_exit.quantity == 3
    assert runner_exit.closes_position is False


def test_stop_exit_closes_all_remaining_through_a_proposal_only(session: Session) -> None:
    _register(session)
    result = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(last_price=Decimal("98.90")),
        evaluated_at=_NOW,
    )
    directive = result.directive
    assert directive is not None
    assert directive.reason is ManagementReason.INITIAL_STOP
    assert directive.leg_id is None
    assert directive.quantity == 9
    assert directive.closes_position is True
    assert directive.proposal.kind is DecisionKind.CLOSE
    assert directive.proposal.target_client_reference == _POSITION
    assert directive.directive_ref != directive.proposal.target_client_reference

    partial = _resolve(
        session,
        directive.directive_ref,
        DirectiveOutcome.PARTIALLY_FILLED_REMAINDER_CANCELLED,
        quantity=Decimal(4),
        price=Decimal("98.85"),
        execution_id="exec-stop-partial",
    )
    assert partial.remaining_quantity == 5
    assert partial.pending_directive is None
    assert partial.flatten_latched_reason is ManagementReason.INITIAL_STOP

    retry_after_rebound = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(
            2,
            as_of=_NOW + timedelta(seconds=2),
            last_price=Decimal("101.00"),
            broker_position_quantity=Decimal(5),
        ),
        evaluated_at=_NOW + timedelta(seconds=2),
    ).directive
    assert retry_after_rebound is not None
    assert retry_after_rebound.reason is ManagementReason.INITIAL_STOP
    assert retry_after_rebound.quantity == 5

    mismatch = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(
            3,
            as_of=_NOW + timedelta(seconds=3),
            last_price=Decimal("98.80"),
            broker_position_quantity=Decimal(6),
        ),
        evaluated_at=_NOW + timedelta(seconds=3),
    )
    assert mismatch.refusal is EvaluationRefusal.RECONCILIATION_MISMATCH


def test_management_event_identity_remains_an_explicit_queue_activation_blocker(
    session: Session,
) -> None:
    _register(session)
    first = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(last_price=Decimal("98.90")),
        evaluated_at=_NOW,
    ).directive
    assert first is not None
    _resolve(
        session,
        first.directive_ref,
        DirectiveOutcome.CANCELLED_NOT_FILLED,
    )
    second = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(
            2,
            as_of=_NOW + timedelta(seconds=2),
            last_price=Decimal("98.90"),
        ),
        evaluated_at=_NOW + timedelta(seconds=2),
    ).directive
    assert second is not None
    assert first.directive_ref != second.directive_ref
    # The existing model-proposal queue intentionally ignores evidence identity
    # to prevent model-authored retry bypasses. Therefore it cannot yet carry
    # these two trusted management events without a dedicated authenticated seam.
    assert economic_fingerprint(first.proposal) == economic_fingerprint(second.proposal)


def test_single_leg_target_2_is_a_complete_close_not_a_cosmetic_reduce(
    session: Session,
) -> None:
    _register(session, quantity=Decimal(2))
    result = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(
            last_price=Decimal(102),
            broker_position_quantity=Decimal(2),
        ),
        evaluated_at=_NOW,
    )
    directive = result.directive
    assert directive is not None
    assert directive.reason is ManagementReason.TARGET_2
    assert directive.leg_id is ManagedLegId.TARGET_2
    assert directive.quantity == 2
    assert directive.closes_position is True
    assert directive.proposal.kind is DecisionKind.CLOSE


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"session_loss_usd": Decimal(60)}, ManagementReason.SESSION_LOSS),
        (
            {
                "marked_strategy_nav_usd": Decimal(1000),
                "session_loss_usd": Decimal(20),
            },
            ManagementReason.SESSION_LOSS,
        ),
        ({"drawdown_fraction": Decimal("0.10")}, ManagementReason.DRAWDOWN),
        ({"opposite_confirmed_regime": True}, ManagementReason.OPPOSITE_REGIME),
    ],
)
def test_circuit_breakers_and_opposite_regime_flatten(
    session: Session, overrides: dict[str, object], reason: ManagementReason
) -> None:
    _register(session)
    result = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(**overrides),
        evaluated_at=_NOW,
    )
    assert result.directive is not None
    assert result.directive.reason is reason
    assert result.directive.leg_id is None


def test_source_default_long_avwap_failure_does_not_invent_an_exit(session: Session) -> None:
    _register(session)
    result = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(long_avwap_failure=True),
        evaluated_at=_NOW,
    )
    assert result.refusal is None
    assert result.directive is None


def test_pending_and_ambiguous_directives_fail_closed_until_reconciled(session: Session) -> None:
    _register(session)
    first = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(last_price=Decimal("98.90")),
        evaluated_at=_NOW,
    )
    directive = first.directive
    assert directive is not None

    pending = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(2, as_of=_NOW + timedelta(seconds=1)),
        evaluated_at=_NOW + timedelta(seconds=1),
    )
    assert pending.refusal is EvaluationRefusal.PENDING_DIRECTIVE

    ambiguous = _resolve(
        session,
        directive.directive_ref,
        DirectiveOutcome.SENT_AMBIGUOUS,
    )
    assert ambiguous.send_ambiguous is True
    blocked = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(3, as_of=_NOW + timedelta(seconds=2)),
        evaluated_at=_NOW + timedelta(seconds=2),
    )
    assert blocked.refusal is EvaluationRefusal.AMBIGUOUS_SEND

    reconciled = _resolve(
        session,
        directive.directive_ref,
        DirectiveOutcome.RECONCILED_NOT_FILLED,
        occurred_at=_NOW + timedelta(seconds=3),
    )
    assert reconciled.pending_directive is None
    assert reconciled.send_ambiguous is False


def test_resolution_quantities_and_phases_are_strict(session: Session) -> None:
    _register(session)
    result = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(last_price=Decimal(101)),
        evaluated_at=_NOW,
    )
    directive = result.directive
    assert directive is not None
    with pytest.raises(PositionManagementError, match="predates its issuance"):
        record_directive_resolution(
            session,
            position_id=_POSITION,
            account_fingerprint=_FINGERPRINT,
            resolution=DirectiveResolution(
                directive_ref=directive.directive_ref,
                outcome=DirectiveOutcome.CANCELLED_NOT_FILLED,
                occurred_at=_NOW - timedelta(microseconds=1),
                evidence_digest="c" * 64,
            ),
        )
    with pytest.raises(PositionManagementError, match="partial-fill"):
        _resolve(
            session,
            directive.directive_ref,
            DirectiveOutcome.PARTIALLY_FILLED_REMAINDER_CANCELLED,
            quantity=Decimal(3),
            price=Decimal(101),
            execution_id="not-partial",
        )
    with pytest.raises(PositionManagementError, match="full-fill"):
        _resolve(
            session,
            directive.directive_ref,
            DirectiveOutcome.FILLED,
            quantity=Decimal(2),
            price=Decimal(101),
            execution_id="not-full",
        )
    with pytest.raises(PositionManagementError, match="invalid for the pending send state"):
        _resolve(
            session,
            directive.directive_ref,
            DirectiveOutcome.RECONCILED_NOT_FILLED,
        )


def test_one_broker_execution_identity_cannot_reduce_two_directives(
    session: Session,
) -> None:
    _register(session)
    first = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(last_price=Decimal(101)),
        evaluated_at=_NOW,
    ).directive
    assert first is not None
    _resolve(
        session,
        first.directive_ref,
        DirectiveOutcome.PARTIALLY_FILLED_REMAINDER_CANCELLED,
        quantity=Decimal(1),
        price=Decimal(101),
        execution_id="exec-replayed",
    )
    second = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(
            2,
            as_of=_NOW + timedelta(seconds=2),
            last_price=Decimal(101),
            broker_position_quantity=Decimal(8),
        ),
        evaluated_at=_NOW + timedelta(seconds=2),
    ).directive
    assert second is not None
    with pytest.raises(PositionManagementError, match="execution_id was already applied"):
        record_directive_resolution(
            session,
            position_id=_POSITION,
            account_fingerprint=_FINGERPRINT,
            resolution=DirectiveResolution(
                directive_ref=second.directive_ref,
                outcome=DirectiveOutcome.FILLED,
                occurred_at=_NOW + timedelta(seconds=3),
                evidence_digest="c" * 64,
                execution_id="exec-replayed",
                fill_quantity=Decimal(2),
                fill_price=Decimal(101),
            ),
        )


def test_resolution_persists_under_the_normalized_account_stream(session: Session) -> None:
    _register(session)
    directive = evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(last_price=Decimal("98.90")),
        evaluated_at=_NOW,
    ).directive
    assert directive is not None
    record_directive_resolution(
        session,
        position_id=_POSITION,
        account_fingerprint=_FINGERPRINT.upper(),
        resolution=DirectiveResolution(
            directive_ref=directive.directive_ref,
            outcome=DirectiveOutcome.FILLED,
            occurred_at=_NOW + timedelta(seconds=1),
            evidence_digest="c" * 64,
            execution_id="exec-normalized-account",
            fill_quantity=Decimal(9),
            fill_price=Decimal("98.90"),
        ),
    )
    replayed = rehydrate_position(
        session,
        account_fingerprint=_FINGERPRINT,
        position_id=_POSITION,
    )
    assert replayed.closed is True


def test_hash_or_semantic_tampering_fails_restart_replay(session: Session) -> None:
    _register(session)
    evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(),
        evaluated_at=_NOW,
    )
    row = session.scalar(select(HashChainRow).where(HashChainRow.kind == "POSITION_EVALUATED"))
    assert row is not None
    row.payload_json = row.payload_json.replace("100.50", "100.51")
    session.flush()
    with pytest.raises(PositionManagementError, match="hash chain is invalid"):
        rehydrate_position(
            session,
            account_fingerprint=_FINGERPRINT,
            position_id=_POSITION,
        )


def test_recomputed_hash_cannot_hide_a_semantically_forged_result(session: Session) -> None:
    _register(session)
    evaluate_position(
        session,
        position_id=_POSITION,
        observation=_observation(),
        evaluated_at=_NOW,
    )
    row = session.scalar(select(HashChainRow).where(HashChainRow.kind == "POSITION_EVALUATED"))
    assert row is not None
    payload = json.loads(row.payload_json)
    payload["evaluation"]["effective_runner_stop_price"] = "100"
    row.payload_json = hash_chain.canonical_payload(payload)
    row.record_hash = hash_chain.compute_hash(
        stream=row.stream,
        sequence=row.sequence,
        recorded_at=row.recorded_at,
        payload_json=row.payload_json,
        previous_hash=row.previous_hash,
    )
    session.flush()
    assert hash_chain.verify(session, row.stream).ok is True
    with pytest.raises(PositionManagementError, match="no longer follows"):
        rehydrate_position(
            session,
            account_fingerprint=_FINGERPRINT,
            position_id=_POSITION,
        )


def test_module_has_no_second_order_or_broker_path() -> None:
    source_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "chronos"
        / "supervisor"
        / "position_management.py"
    )
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    forbidden = ("chronos.broker", "chronos.orders", "chronos.execution", "ib_async", "ibapi")
    assert not {
        name
        for name in imports
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    }
    assert "to_order_request" not in source
    assert "submit_order" not in source


def test_module_is_not_wired_into_the_production_runtime() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src" / "chronos"
    module_path = source_root / "supervisor" / "position_management.py"
    importers: list[str] = []
    for path in source_root.rglob("*.py"):
        if path == module_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imports_module = isinstance(node, ast.ImportFrom) and (
                node.module == "chronos.supervisor.position_management"
            )
            imports_module_directly = isinstance(node, ast.Import) and any(
                alias.name == "chronos.supervisor.position_management" for alias in node.names
            )
            if imports_module or imports_module_directly:
                importers.append(str(path.relative_to(source_root)))
    assert importers == []


def test_module_defines_no_parallel_authority_grant() -> None:
    source_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "chronos"
        / "supervisor"
        / "position_management.py"
    )
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "PaperPositionManagementAuthority" not in source
    assert "owner_authorization_ref" not in source
    assert "AutonomyMode" not in source

    authority_words = ("authority", "mandate", "grant", "enable")
    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    parameter_names: set[str] = set()
    field_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
            parameter_names.update(argument.arg for argument in arguments)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            field_names.add(node.target.id)
    assert not {
        name
        for name in class_names | parameter_names
        if any(word in name.lower() for word in authority_words)
    }
    assert {
        name for name in field_names if any(word in name.lower() for word in authority_words)
    } == {"execution_authority"}
