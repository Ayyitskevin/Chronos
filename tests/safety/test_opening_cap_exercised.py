"""The daily opening cap, finally exercised end to end (M10, closes R-25).

``max_opening_orders_per_day`` shipped in Milestone 5 and had never refused
anything. Two independent defects kept it inert, and each alone was sufficient:

1. ``BrokerRiskEvidenceProvider.gather`` never set ``opening_orders_today``, so
   the field took its ``0`` default and ``0 + 1 <= limit`` was true on every
   call, forever. ADR-0010 §4 asserted this was wired. It was not.
2. ``OrderIntentRepository.count_opening_since`` — which had zero callers — also
   filtered ``action == SELL``. ``OPEN ∧ SELL`` is exactly ``{OPEN_SHORT_PUT,
   OPEN_COVERED_CALL}``, so every stock and crypto opening was invisible to the
   cap even after it was wired.

So this file drives the whole path: intents written to a real repository, read
back through the provider that computes the market-local day boundary, into the
risk check that is supposed to refuse. It asserts the outcome that had never
once happened — a refusal — plus the two directions the fix must not get wrong:
unknown must not read as zero, and the day must be the market's, not UTC's.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from chronos.config.settings import Settings
from chronos.domain.enums import (
    OrderIntent,
    OrderLifecycle,
    OrderSide,
    ProductFamily,
    RiskCheckStatus,
)
from chronos.domain.models import UnderlyingContract
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import build_stock_intent
from chronos.orders.risk import OrderRiskEngine, RiskEvidence
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import OrderIntentRecord, OrderIntentRepository
from chronos.utils.identifiers import account_fingerprint
from tests.support.order_fakes import FakeBroker

_ACCOUNT = "DU1234567"
_NY = ZoneInfo("America/New_York")

# The instant that separates a market-local day boundary from a UTC one. 21:00
# in New York on the 16th is already 01:00 UTC on the 17th, so a UTC midnight
# would treat the whole preceding session as "yesterday" and hand back a fresh
# allowance for the evening.
_EVENING = datetime(2026, 7, 16, 21, 0, tzinfo=_NY).astimezone(UTC)
_MORNING = datetime(2026, 7, 16, 10, 0, tzinfo=_NY).astimezone(UTC)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None, "max_opening_orders_per_day": 3}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _database() -> Database:
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.bind_scope(broker_mode="ibkr", environment="paper", account_id=_ACCOUNT)
    return db


def _record(
    intent_id: str,
    *,
    created_at: datetime,
    effect: str = "OPEN",
    action: OrderSide = OrderSide.BUY,
    family: ProductFamily = ProductFamily.STOCK,
) -> OrderIntentRecord:
    return OrderIntentRecord(
        intent_id=intent_id,
        idempotency_key=f"key-{intent_id}",
        account_fingerprint=account_fingerprint(_ACCOUNT),
        environment="paper",
        product_family=family,
        wheel_cycle_id=None,
        symbol="SPY",
        con_id=756733,
        local_symbol=None,
        action=action,
        open_close_effect=effect,
        quantity=Decimal("1"),
        order_type="LMT",
        limit_price=Decimal("400.00"),
        time_in_force="DAY",
        outside_rth=False,
        quote_snapshot_id=None,
        risk_snapshot_id=None,
        preview_id=None,
        confirmation_hash=None,
        order_ref=f"CHR-ORD-{intent_id}",
        status=OrderLifecycle.DRAFT,
        created_at=created_at,
        confirmed_at=None,
        submitted_at=None,
        expires_at=None,
    )


def _intent() -> Any:
    return build_stock_intent(
        account_id=_ACCOUNT,
        intent=OrderIntent.OPEN_LONG_STOCK,
        contract=UnderlyingContract(con_id=756733, symbol="SPY"),
        quantity=1,
        limit_price=Decimal("400.00"),
    )


def _provider(intents: Any, **settings: object) -> BrokerRiskEvidenceProvider:
    """A provider built for one method.

    ``_opening_today`` reads the repository and the configured timezone and
    nothing else — no connection, no gateway. The count costs one indexed
    ``COUNT(*)``, which is why it can sit on the order path at all.
    """

    return BrokerRiskEvidenceProvider(
        connection=None,  # type: ignore[arg-type]
        settings=_settings(**settings),
        intents=intents,
    )


class _Exploding:
    """A repository that fails the way a real one fails: at call time."""

    def count_opening_since(self, *, current_account_id: str, since: datetime) -> int:
        raise RuntimeError("database is gone")


class _Connection:
    """Enough connection to let ``gather`` do its three broker reads."""

    def __init__(self) -> None:
        self.broker = FakeBroker()

    def run(self, coroutine: Any, *, timeout: float | None = None) -> Any:
        del timeout
        return asyncio.run(coroutine)


# --- the repository half of the defect ----------------------------------------


def test_a_buy_opening_is_counted_at_all() -> None:
    """The half that made the cap blind to stocks and crypto.

    Under the old ``action == SELL`` filter this returned 0, so a strategy that
    only ever bought stock or crypto could open without limit while the check
    reported a passing count of one.
    """

    db = _database()
    intents = OrderIntentRepository(db.sessions)
    intents.create(_record("buy-1", created_at=_MORNING), current_account_id=_ACCOUNT)
    assert intents.count_opening_since(current_account_id=_ACCOUNT, since=_MORNING) == 1


def test_closing_intents_are_never_counted() -> None:
    """The cap bounds new exposure, not the unwinding of it.

    Counting closes would throttle exactly the orders that reduce risk, and
    would do it hardest on the day the loop had been busiest.
    """

    db = _database()
    intents = OrderIntentRepository(db.sessions)
    intents.create(
        _record("close-1", created_at=_MORNING, effect="CLOSE", action=OrderSide.SELL),
        current_account_id=_ACCOUNT,
    )
    assert intents.count_opening_since(current_account_id=_ACCOUNT, since=_MORNING) == 0


def test_every_opening_family_lands_in_the_same_counter() -> None:
    """One cap over the account, not one cap per family.

    ``open_close_effect`` is the only predicate that means "this creates
    exposure", so a short put, a long stock buy and a crypto buy all consume the
    same daily allowance.
    """

    db = _database()
    intents = OrderIntentRepository(db.sessions)
    for index, (action, family) in enumerate(
        (
            (OrderSide.SELL, ProductFamily.OPTION),
            (OrderSide.BUY, ProductFamily.STOCK),
            (OrderSide.BUY, ProductFamily.CRYPTO),
        )
    ):
        intents.create(
            _record(f"mixed-{index}", created_at=_MORNING, action=action, family=family),
            current_account_id=_ACCOUNT,
        )
    assert intents.count_opening_since(current_account_id=_ACCOUNT, since=_MORNING) == 3


# --- the day boundary ---------------------------------------------------------


def test_the_day_is_the_markets_not_utcs() -> None:
    """A UTC boundary would hand out a second allowance every evening.

    Three openings placed during the New York session, then a look at 21:00 New
    York — which is already tomorrow in UTC. Under a UTC midnight the counter
    reads 0 and the cap silently doubles for anyone trading into the evening,
    and crypto trades 24/7, so "the evening" is not a corner case here.
    """

    db = _database()
    intents = OrderIntentRepository(db.sessions)
    for index in range(3):
        intents.create(_record(f"day-{index}", created_at=_MORNING), current_account_id=_ACCOUNT)

    counted = _provider(intents)._opening_today(_intent(), now=_EVENING)
    assert counted == 3

    utc_midnight = _EVENING.replace(hour=0, minute=0, second=0, microsecond=0)
    assert intents.count_opening_since(current_account_id=_ACCOUNT, since=utc_midnight) == 0


def test_yesterdays_openings_do_not_bind_today() -> None:
    """The counter has to reset, or the cap becomes a lifetime budget."""

    db = _database()
    intents = OrderIntentRepository(db.sessions)
    yesterday = datetime(2026, 7, 15, 10, 0, tzinfo=_NY).astimezone(UTC)
    for index in range(3):
        intents.create(_record(f"old-{index}", created_at=yesterday), current_account_id=_ACCOUNT)
    assert _provider(intents)._opening_today(_intent(), now=_EVENING) == 0


# --- unknown is not zero ------------------------------------------------------


@pytest.mark.parametrize("repository", [None, _Exploding()])
def test_an_uncountable_day_reads_unknown_not_zero(repository: Any) -> None:
    """The direction that matters.

    A provider with no repository, or one whose repository just died, must not
    report full headroom. "We could not ask" is not "no" — and a cap that reads
    "0 used" precisely when it cannot see is a cap that has stopped existing.
    """

    assert _provider(repository)._opening_today(_intent(), now=_EVENING) is None


def test_evidence_defaults_to_unknown_rather_than_an_empty_day() -> None:
    """The field default is the same trap one layer down.

    ``opening_orders_today: int = 0`` was half of why this check never fired:
    any evidence that forgot to set it asserted an empty trading day. The
    default is now ``None``, so forgetting is a refusal instead of a pass.
    """

    assert RiskEvidence.model_fields["opening_orders_today"].default is None


# --- the check itself ---------------------------------------------------------


def _opening_check(evidence_count: int | None, *, limit: int = 3) -> Any:
    engine = OrderRiskEngine(_settings(max_opening_orders_per_day=limit))
    return engine._check_opening_count(
        RiskEvidence.model_construct(opening_orders_today=evidence_count)
    )


def test_the_cap_finally_refuses() -> None:
    """The assertion that could not have been written before M10.

    With the limit reached, the next opening is a FAIL. Every prior release
    returned PASS here no matter what the loop had already done.
    """

    check = _opening_check(3)
    assert check.status is RiskCheckStatus.FAIL
    assert "daily cap" in check.detail


def test_headroom_still_passes() -> None:
    assert _opening_check(2).status is RiskCheckStatus.PASS


def test_an_unknown_count_blocks_rather_than_passing() -> None:
    """UNKNOWN, not FAIL and not PASS.

    The engine treats both non-PASS states as blocking, but the distinction is
    load-bearing for the operator reading the refusal: "the cap is reached" and
    "the cap could not be evaluated" call for different responses.
    """

    check = _opening_check(None)
    assert check.status is RiskCheckStatus.UNKNOWN
    assert "unavailable" in check.detail


# --- the wire between them ----------------------------------------------------


def _gathered(intents: Any) -> RiskEvidence:
    provider = BrokerRiskEvidenceProvider(
        connection=cast(Any, _Connection()),
        settings=_settings(),
        intents=intents,
    )
    return provider.gather(_intent(), now=_EVENING)


def test_gather_actually_carries_the_count_into_the_evidence() -> None:
    """The defect was here, and only a real ``gather`` call can see it.

    Every other test in this file would still pass if ``gather`` computed the
    count and then dropped it on the floor — which is precisely what the code
    did for five milestones. This one asserts the value reaches the field the
    engine reads.
    """

    db = _database()
    intents = OrderIntentRepository(db.sessions)
    for index in range(2):
        intents.create(_record(f"wired-{index}", created_at=_MORNING), current_account_id=_ACCOUNT)
    assert _gathered(intents).opening_orders_today == 2


def test_gather_without_a_repository_yields_unknown_not_zero() -> None:
    """The same wire, in the direction that has to fail closed."""

    assert _gathered(None).opening_orders_today is None


def test_a_zero_limit_refuses_the_first_opening() -> None:
    """``max_opening_orders_per_day=0`` means no openings, not one.

    The check is ``used + 1 <= limit``, so this is the boundary where an
    off-by-one would let exactly one order through a configuration that asked
    for none.
    """

    assert _opening_check(0, limit=0).status is RiskCheckStatus.FAIL
