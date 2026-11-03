"""U3F1: exclude the evaluated persisted order, never an arbitrary reused id."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from chronos.domain.enums import IBEnvironment, OrderLifecycle, RiskCheckStatus
from chronos.orders.intent import WheelOrderIntent
from chronos.persistence.order_repositories import OrderIntentRepository
from tests.safety.test_opening_cap_exercised import (
    _ACCOUNT,
    _MORNING,
    _database,
    _intent,
    _opening_check,
    _provider,
    _record,
)
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY


def _persist(repo: OrderIntentRepository, intent: WheelOrderIntent) -> None:
    repo.create(
        intent.to_intent_record(
            environment=IBEnvironment.PAPER,
            status=OrderLifecycle.DRAFT,
            created_at=_MORNING,
            expires_at=None,
            day_bucket="2026-07-16",
            key=TEST_PERSISTENCE_KEY,
        ),
        current_account_id=_ACCOUNT,
    )


@pytest.mark.parametrize(
    "prior, expected",
    [(2, RiskCheckStatus.PASS), (3, RiskCheckStatus.FAIL), (4, RiskCheckStatus.FAIL)],
)
def test_limit_boundary_counts_only_prior_attempts(prior: int, expected: RiskCheckStatus) -> None:
    db = _database()
    repo = OrderIntentRepository(db.sessions)
    for i in range(prior):
        # Rejected attempts are still capacity consumers.
        repo.create(
            replace(_record(f"prior-{i}", created_at=_MORNING), status=OrderLifecycle.REJECTED),
            current_account_id=_ACCOUNT,
        )
    intent = _intent()
    _persist(repo, intent)
    count = _provider(repo)._opening_today(intent, now=_MORNING)
    assert count == prior
    assert _opening_check(count, limit=3).status is expected
    assert repo.count_opening_since(current_account_id=_ACCOUNT, since=_MORNING) == prior + 1


def test_one_opening_is_allowed_at_a_cap_of_one() -> None:
    db = _database()
    repo = OrderIntentRepository(db.sessions)
    intent = _intent()
    _persist(repo, intent)
    count = _provider(repo)._opening_today(intent, now=_MORNING)
    assert count == 0
    assert _opening_check(count, limit=1).status is RiskCheckStatus.PASS


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", Decimal("2")),
        ("limit_price", Decimal("401")),
        ("correlation_id", "CHR-ORD-OTHER"),
        ("outside_rth", True),
    ],
)
def test_reused_id_with_different_economics_is_not_excluded(field: str, value: object) -> None:
    db = _database()
    repo = OrderIntentRepository(db.sessions)
    intent = _intent()
    _persist(repo, intent)
    forged = intent.model_copy(update={field: value})
    count = _provider(repo)._opening_today(forged, now=_MORNING)
    assert count is None
    assert _opening_check(count).status is RiskCheckStatus.UNKNOWN
    assert repo.count_opening_since(current_account_id=_ACCOUNT, since=_MORNING) == 1


def test_unpersisted_intent_does_not_exclude_another_row() -> None:
    db = _database()
    repo = OrderIntentRepository(db.sessions)
    _persist(repo, _intent())
    assert _provider(repo)._opening_today(_intent(), now=_MORNING) == 1


def test_foreign_account_cannot_exclude_the_persisted_intent() -> None:
    db = _database()
    repo = OrderIntentRepository(db.sessions)
    intent = _intent()
    _persist(repo, intent)
    forged = intent.model_copy(update={"account_id": "DU7654321"})
    assert _provider(repo)._opening_today(forged, now=_MORNING) is None


def test_repository_rejects_a_foreign_evaluation_in_a_valid_count_scope() -> None:
    db = _database()
    repo = OrderIntentRepository(db.sessions)
    intent = _intent()
    _persist(repo, intent)
    with pytest.raises(ValueError, match="account"):
        repo.count_opening_since(
            current_account_id=_ACCOUNT,
            since=_MORNING,
            evaluated_intent=intent.model_copy(update={"account_id": "DU7654321"}),
        )


def test_identical_persisted_evaluation_can_be_repeated_without_waiving_other_rows() -> None:
    db = _database()
    repo = OrderIntentRepository(db.sessions)
    repo.create(_record("prior", created_at=_MORNING), current_account_id=_ACCOUNT)
    intent = _intent()
    _persist(repo, intent)
    for _ in range(2):
        assert _provider(repo)._opening_today(intent, now=_MORNING) == 1
    assert repo.count_opening_since(current_account_id=_ACCOUNT, since=_MORNING) == 2
