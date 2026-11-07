"""RECHECK-345 P3-2: the evaluated row's environment is compared with the CONFIGURED one.

``count_opening_since`` used to build its expected signature from ``row.environment``,
so that element of the identity tuple was compared with itself and could never fail.
A persisted candidate stamped with another environment than the one this process is
configured for must not be excluded from the opening count; the count refuses instead.
"""

from __future__ import annotations

import pytest

from chronos.domain.enums import IBEnvironment, OrderLifecycle
from chronos.persistence.order_repositories import OrderIntentRepository
from tests.safety.test_opening_cap_exercised import (
    _ACCOUNT,
    _MORNING,
    _database,
    _intent,
    _provider,
)
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY


def _persist(repo: OrderIntentRepository, intent: object, environment: IBEnvironment) -> None:
    repo.create(
        intent.to_intent_record(  # type: ignore[attr-defined]
            environment=environment,
            status=OrderLifecycle.DRAFT,
            created_at=_MORNING,
            expires_at=None,
            day_bucket="2026-07-16",
            key=TEST_PERSISTENCE_KEY,
        ),
        current_account_id=_ACCOUNT,
    )


def test_a_row_from_another_environment_is_not_excluded_as_the_evaluated_intent() -> None:
    db = _database()
    repo = OrderIntentRepository(db.sessions)
    intent = _intent()
    _persist(repo, intent, IBEnvironment.LIVE)  # row says LIVE; the provider is configured PAPER
    assert _provider(repo)._opening_today(intent, now=_MORNING) is None


def test_the_matching_environment_is_still_excluded() -> None:
    db = _database()
    repo = OrderIntentRepository(db.sessions)
    intent = _intent()
    _persist(repo, intent, IBEnvironment.PAPER)
    assert _provider(repo)._opening_today(intent, now=_MORNING) == 0


def test_the_repository_refuses_an_evaluation_without_a_configured_environment() -> None:
    db = _database()
    repo = OrderIntentRepository(db.sessions)
    intent = _intent()
    _persist(repo, intent, IBEnvironment.PAPER)
    with pytest.raises(ValueError, match="environment"):
        repo.count_opening_since(
            current_account_id=_ACCOUNT, since=_MORNING, evaluated_intent=intent, environment=None
        )
    with pytest.raises(ValueError, match="persisted identity"):
        repo.count_opening_since(
            current_account_id=_ACCOUNT,
            since=_MORNING,
            evaluated_intent=intent,
            environment=IBEnvironment.LIVE.value,
        )
