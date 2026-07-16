from datetime import UTC, datetime
from decimal import Decimal

import pytest

from chronos.config.settings import Settings
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    ReconciliationStatus,
    WheelStage,
)
from chronos.domain.models import AccountSummary, ConnectionStatus
from chronos.services.reconciliation import (
    ReconciliationAccountView,
    ReconciliationResult,
    ReconciliationSnapshotView,
    SymbolReconciliation,
)
from chronos.ui.portfolio_state import (
    PortfolioObservationSessionRecord,
    retain_portfolio_observation,
    validate_portfolio_observation_record,
)
from chronos.ui.runtime_scope import RuntimeScopeView, build_bound_runtime_scope

NOW = datetime(2026, 7, 16, 15, 30, tzinfo=UTC)
ACCOUNT_ID = "DU1234567"


class _ScopeBinder:
    def bind_scope(
        self,
        *,
        broker_mode: str,
        environment: str,
        account_id: str,
    ) -> None:
        pass


def _runtime_scope(*, account_id: str = ACCOUNT_ID) -> RuntimeScopeView:
    return build_bound_runtime_scope(
        _ScopeBinder(),
        Settings.model_validate({}),
        AccountSummary(
            account_id=account_id,
            net_liquidation=Decimal("250000"),
            total_cash=Decimal("125000"),
            buying_power=Decimal("240000"),
            as_of=NOW,
        ),
        ConnectionStatus(
            state=ConnectionState.CONNECTED,
            environment=DisplayEnvironment.DEMO,
            connected=True,
            account_id=account_id,
            data_quality=DataQuality.DEMO,
            last_successful_sync=NOW,
        ),
    )


def _symbol(
    *,
    status: ReconciliationStatus = ReconciliationStatus.RECONCILED,
    manual_review_required: bool = False,
    reasons: tuple[str, ...] = (),
) -> SymbolReconciliation:
    return SymbolReconciliation(
        symbol="AAPL",
        status=status,
        stage=(
            WheelStage.FLAT
            if status is ReconciliationStatus.RECONCILED
            else WheelStage.MANUAL_REVIEW
        ),
        stock_shares=Decimal("0"),
        unencumbered_shares=Decimal("0"),
        short_put_contracts=Decimal("0"),
        short_call_contracts=Decimal("0"),
        pending_put_contracts=Decimal("0"),
        pending_call_contracts=Decimal("0"),
        manual_review_required=manual_review_required,
        reasons=reasons,
    )


def _result() -> ReconciliationResult:
    return ReconciliationResult(
        status=ReconciliationStatus.RECONCILED,
        snapshot=ReconciliationSnapshotView(
            environment=DisplayEnvironment.DEMO,
            data_quality=DataQuality.DEMO,
            account=ReconciliationAccountView(
                masked_account_id="DU•••4567",
                net_liquidation=Decimal("250000"),
                total_cash=Decimal("125000"),
                buying_power=Decimal("240000"),
                currency="USD",
                as_of=NOW,
            ),
            positions=(),
            open_orders=(),
            execution_count=0,
            server_time_start=NOW,
            server_time_end=NOW,
            captured_at=NOW,
            window_seconds=0,
            server_window_seconds=0,
        ),
        symbols=(_symbol(),),
        reasons=(),
    )


def test_retained_observation_is_scope_bound_historical_and_non_authoritative() -> None:
    scope = _runtime_scope()

    record = retain_portfolio_observation(scope, _result())

    assert len(record.scope_digest) == 64
    assert record.historical_display_only is True
    assert record.authority_created is False
    assert record.persistence_recorded is False
    assert record.opening_actions_locked is True
    assert validate_portfolio_observation_record(record, scope) == record
    assert ACCOUNT_ID not in record.model_dump_json()


def test_pending_observation_without_snapshot_remains_valid_and_locked() -> None:
    scope = _runtime_scope()
    reason = "Broker evidence could not be captured; reconciliation remains locked."
    pending = ReconciliationResult(
        status=ReconciliationStatus.PENDING,
        snapshot=None,
        symbols=(
            _symbol(
                status=ReconciliationStatus.PENDING,
                manual_review_required=True,
                reasons=(reason,),
            ),
        ),
        reasons=(reason,),
    )

    record = retain_portfolio_observation(scope, pending)

    assert record.result.status is ReconciliationStatus.PENDING
    assert record.result.snapshot is None
    assert record.opening_actions_locked is True


def test_pending_observation_with_stable_snapshot_remains_valid_and_locked() -> None:
    scope = _runtime_scope()
    reason = "Local strategy evidence is incomplete; reconciliation remains locked."
    base = _result()
    pending = base.model_copy(
        update={
            "status": ReconciliationStatus.PENDING,
            "symbols": (
                _symbol(
                    status=ReconciliationStatus.PENDING,
                    manual_review_required=True,
                    reasons=(reason,),
                ),
            ),
            "reasons": (reason,),
        }
    )

    record = retain_portfolio_observation(scope, pending)

    assert record.result.status is ReconciliationStatus.PENDING
    assert record.result.snapshot is not None
    assert record.opening_actions_locked is True


def test_record_rejects_changed_runtime_scope() -> None:
    record = retain_portfolio_observation(_runtime_scope(), _result())

    with pytest.raises(ValueError, match="record is unavailable"):
        validate_portfolio_observation_record(
            record,
            _runtime_scope(account_id="DU7654321"),
        )


@pytest.mark.parametrize(
    "currency",
    ["", " ", "\x00", "usd", "USDOLLARS"],
)
def test_record_rejects_unrenderable_account_currency(currency: str) -> None:
    scope = _runtime_scope()
    result = _result()
    assert result.snapshot is not None
    snapshot = result.snapshot.model_copy(
        update={"account": result.snapshot.account.model_copy(update={"currency": currency})}
    )
    forged = result.model_copy(update={"snapshot": snapshot})

    with pytest.raises(ValueError, match="result is unavailable"):
        retain_portfolio_observation(scope, forged)


@pytest.mark.parametrize(
    "case",
    [
        "unlock",
        "authority",
        "persistence",
        "scope_digest",
        "aggregate_status",
        "duplicate_symbol",
        "snapshot_environment",
        "snapshot_account",
        "raw_reason",
        "extreme_time",
    ],
)
def test_record_rejects_forged_or_incoherent_session_state(case: str) -> None:
    scope = _runtime_scope()
    record = retain_portfolio_observation(scope, _result())
    result = record.result

    if case == "unlock":
        forged = record.model_copy(update={"opening_actions_locked": False})
    elif case == "authority":
        forged = record.model_copy(update={"authority_created": True})
    elif case == "persistence":
        forged = record.model_copy(update={"persistence_recorded": True})
    elif case == "scope_digest":
        forged = record.model_copy(update={"scope_digest": "0" * 64})
    elif case == "aggregate_status":
        forged = record.model_copy(
            update={"result": result.model_copy(update={"status": ReconciliationStatus.PENDING})}
        )
    elif case == "duplicate_symbol":
        forged = record.model_copy(
            update={"result": result.model_copy(update={"symbols": result.symbols * 2})}
        )
    else:
        assert result.snapshot is not None
        snapshot = result.snapshot
        if case == "snapshot_environment":
            snapshot = snapshot.model_copy(update={"environment": DisplayEnvironment.PAPER})
        elif case == "snapshot_account":
            snapshot = snapshot.model_copy(
                update={
                    "account": snapshot.account.model_copy(update={"masked_account_id": ACCOUNT_ID})
                }
            )
        elif case == "raw_reason":
            result = result.model_copy(update={"reasons": (f"Account {ACCOUNT_ID} changed.",)})
        else:
            snapshot = snapshot.model_copy(update={"captured_at": datetime.min.replace(tzinfo=UTC)})
        if case != "raw_reason":
            result = result.model_copy(update={"snapshot": snapshot})
        forged = record.model_copy(update={"result": result})

    with pytest.raises(ValueError, match="record is unavailable"):
        validate_portfolio_observation_record(forged, scope)


def test_record_requires_exact_top_level_and_nested_model_storage() -> None:
    scope = _runtime_scope()
    record = retain_portfolio_observation(scope, _result())

    class _RecordSubclass(PortfolioObservationSessionRecord):
        pass

    class _ResultSubclass(ReconciliationResult):
        pass

    subclass_record = _RecordSubclass.model_validate(record.model_dump())
    nested_subclass = _ResultSubclass.model_validate(record.result.model_dump())
    forged_nested = record.model_copy(update={"result": nested_subclass})
    extra_storage = record.model_copy()
    vars(extra_storage)["unexpected"] = True

    for value in (subclass_record, forged_nested, extra_storage):
        with pytest.raises(ValueError, match="record is unavailable"):
            validate_portfolio_observation_record(value, scope)
