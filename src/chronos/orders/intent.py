"""The Milestone 5 order-intent value object and its idempotency key.

Named ``WheelOrderIntent`` to avoid colliding with the two existing
``OrderIntent`` names (the domain StrEnum and the autonomous
``chronos.execution.intents.OrderIntent`` dataclass). It is the single
economic description of a proposed order that flows through risk → preview →
confirmation → the submission boundary, and it converts to both the broker
:class:`~chronos.domain.models.OrderRequest` (with ``transmit`` defaulting to
False) and the persisted
:class:`~chronos.persistence.order_repositories.OrderIntentRecord`.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import field_validator, model_validator

from chronos.domain.enums import (
    IBEnvironment,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
    ProductFamily,
)
from chronos.domain.models import (
    ChronosModel,
    CryptoContract,
    Instrument,
    OptionContract,
    OrderRequest,
    UnderlyingContract,
)
from chronos.persistence.order_repositories import OrderIntentRecord
from chronos.utils.identifiers import account_fingerprint

OrderRefStr = str

_CORRELATION_PREFIX = "CHR-ORD-"
_OPEN_INTENTS: frozenset[OrderIntent] = frozenset(
    {
        OrderIntent.OPEN_SHORT_PUT,
        OrderIntent.OPEN_COVERED_CALL,
        OrderIntent.OPEN_LONG_STOCK,
        OrderIntent.OPEN_LONG_CRYPTO,
    }
)
_SELL_INTENTS: frozenset[OrderIntent] = frozenset(
    {
        OrderIntent.OPEN_SHORT_PUT,
        OrderIntent.OPEN_COVERED_CALL,
        OrderIntent.CLOSE_LONG_STOCK,
        OrderIntent.CLOSE_LONG_CRYPTO,
    }
)
# A uuid5 namespace fixed for Chronos order idempotency (never security-bearing).
_IDEMPOTENCY_NAMESPACE = uuid.UUID("6f2a1e14-1d2b-5c3a-9f77-c0ffee5100d5")
# Upper magnitude bound: Numeric(20,8) allows 12 integer digits, so a quantity
# must be strictly below 1e12 (ADR-0010 §1). Pairs with the -8 scale check to
# bound the persisted/hashed/transmitted quantity to exactly Numeric(20,8).
_MAX_QUANTITY = Decimal(10) ** 12


def side_for_intent(intent: OrderIntent) -> OrderSide:
    return OrderSide.SELL if intent in _SELL_INTENTS else OrderSide.BUY


def open_close_effect_for_intent(intent: OrderIntent) -> Literal["OPEN", "CLOSE"]:
    return "OPEN" if intent in _OPEN_INTENTS else "CLOSE"


def new_correlation_id() -> OrderRefStr:
    """Return a fresh Chronos-owned order reference (``CHR-ORD-<32 hex>``)."""

    return _CORRELATION_PREFIX + uuid.uuid4().hex.upper()


def canonical_quantity(quantity: Decimal) -> str:
    """Canonical, exponent-free quantity text (ADR-0010 §1).

    ``normalize()`` collapses exponent/trailing-zero variants so economically
    identical spellings can never fork an idempotency key or confirmation
    hash; ``format(..., "f")`` never emits E-notation and is byte-identical to
    the pre-M7C ``str(int)`` for every integral quantity (golden-pinned).
    NOTE: ``limit_price`` deliberately does NOT use this (changing it would
    alter existing hashes); its trailing-zero fork is a recorded limitation.
    """

    return format(quantity.normalize(), "f")


class WheelOrderIntent(ChronosModel):
    """One proposed order, family-aware, prior to any broker interaction."""

    intent_id: str
    correlation_id: OrderRefStr
    account_id: str
    product_family: ProductFamily
    intent: OrderIntent
    contract: Instrument
    quantity: Decimal
    limit_price: Decimal
    order_type: Literal["LMT"] = "LMT"
    # Family-conditional TIF (ADR-0010 §3): OPTION/STOCK are always DAY; CRYPTO
    # may be DAY or IOC per the owner-verified setting. Widening the Literal
    # does not change existing hashes — DAY intents still serialize "DAY".
    time_in_force: Literal["DAY", "IOC"] = "DAY"
    outside_rth: bool = False

    @field_validator("correlation_id")
    @classmethod
    def validate_correlation_id(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized.startswith(_CORRELATION_PREFIX):
            raise ValueError("correlation_id must be a Chronos-owned CHR-ORD- reference")
        return normalized

    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("quantity must be a finite number")
        if value <= 0:
            raise ValueError("quantity must be positive")
        exponent = value.normalize().as_tuple().exponent
        if isinstance(exponent, int) and exponent < -8:
            # Numeric(20,8) is the persistence scale (ADR-0010 §1): anything
            # finer would round-trip lossily and desynchronize the audit
            # record from the hashed/transmitted quantity.
            raise ValueError("quantity is finer than the 1e-8 persistence scale")
        if value >= _MAX_QUANTITY:
            # Upper magnitude bound, the other half of Numeric(20,8) (12 integer
            # digits). Without it a huge quantity (a) overflows the 28-digit
            # Decimal context in the venue-conformance modulo, raising an
            # ArithmeticError that escapes the "engine never raises" contract,
            # and (b) would be rounded by canonical_quantity's normalize(),
            # forking the idempotency key/confirmation hash from the true value.
            raise ValueError("quantity exceeds the Numeric(20,8) persistence magnitude")
        return value

    @field_validator("limit_price")
    @classmethod
    def validate_limit_price(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("limit_price must be positive (market orders are impossible)")
        return value

    @field_validator("account_id")
    @classmethod
    def validate_account_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("account_id must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_family_and_contract(self) -> WheelOrderIntent:
        if self.product_family is ProductFamily.OPTION:
            if not isinstance(self.contract, OptionContract):
                raise ValueError("OPTION intents require an OptionContract")
            if self.intent not in {
                OrderIntent.OPEN_SHORT_PUT,
                OrderIntent.OPEN_COVERED_CALL,
                OrderIntent.CLOSE_SHORT_OPTION,
            }:
                raise ValueError("OPTION intents must be a short-put/covered-call/close intent")
            self._require_whole_units()
        elif self.product_family is ProductFamily.STOCK:
            if not isinstance(self.contract, UnderlyingContract):
                raise ValueError("STOCK intents require an UnderlyingContract")
            if self.intent not in {OrderIntent.OPEN_LONG_STOCK, OrderIntent.CLOSE_LONG_STOCK}:
                raise ValueError("STOCK intents must be OPEN_LONG_STOCK or CLOSE_LONG_STOCK")
            self._require_whole_units()
        elif self.product_family is ProductFamily.CRYPTO:
            # Fractional Decimal quantities are permitted here (ADR-0010 §1) —
            # the whole-unit check is deliberately NOT applied to crypto. The
            # venue min-size/size-increment conformance is enforced downstream
            # in the crypto risk branch, from qualified contract metadata.
            if not isinstance(self.contract, CryptoContract):
                raise ValueError("CRYPTO intents require a CryptoContract")
            if self.intent not in {OrderIntent.OPEN_LONG_CRYPTO, OrderIntent.CLOSE_LONG_CRYPTO}:
                raise ValueError("CRYPTO intents must be OPEN_LONG_CRYPTO or CLOSE_LONG_CRYPTO")
        else:  # pragma: no cover - exhaustive over ProductFamily; fail closed
            raise ValueError(f"unsupported product family {self.product_family.value}")
        if self.contract.symbol != self.contract.symbol.strip().upper():
            raise ValueError("contract symbol must be normalized")
        return self

    def _require_whole_units(self) -> None:
        # ADR-0010 §1: fractional quantities are a CRYPTO-only capability;
        # options and stocks stay whole-unit (first line of the defense in
        # depth — the stock risk check re-asserts this as a genuine FAIL).
        if self.quantity != self.quantity.to_integral_value():
            raise ValueError(f"{self.product_family.value} quantities must be whole units")

    @property
    def side(self) -> OrderSide:
        return side_for_intent(self.intent)

    @property
    def open_close_effect(self) -> Literal["OPEN", "CLOSE"]:
        return open_close_effect_for_intent(self.intent)

    @property
    def symbol(self) -> str:
        return self.contract.symbol

    def to_order_request(self, *, transmit: bool = False) -> OrderRequest:
        """Build the broker request. ``transmit`` is False everywhere but the boundary."""

        return OrderRequest(
            correlation_id=self.correlation_id,
            account_id=self.account_id,
            contract=self.contract,
            intent=self.intent,
            side=self.side,
            quantity=self.quantity,
            limit_price=self.limit_price,
            order_ref=self.correlation_id,
            transmit=transmit,
            time_in_force=self.time_in_force,
            outside_rth=self.outside_rth,
        )

    def idempotency_key(self, *, day_bucket: str) -> str:
        """Deterministic key over the order's economic content and trading day.

        A genuinely different order (different contract/side/qty/price) yields a
        different key, so it is never falsely suppressed; a true duplicate on
        the same trading day collides and is refused by the unique constraint.
        """

        con_id = getattr(self.contract, "con_id", 0)
        parts = (
            account_fingerprint(self.account_id),
            self.product_family.value,
            self.symbol,
            str(con_id),
            self.intent.value,
            side_for_intent(self.intent).value,
            open_close_effect_for_intent(self.intent),
            canonical_quantity(self.quantity),
            format(self.limit_price, "f"),
            day_bucket,
        )
        payload = "\x1f".join(f"{len(part)}:{part}" for part in parts)
        return uuid.uuid5(_IDEMPOTENCY_NAMESPACE, payload).hex

    def to_intent_record(
        self,
        *,
        environment: IBEnvironment,
        status: OrderLifecycle,
        created_at: datetime,
        expires_at: datetime | None,
        day_bucket: str,
        wheel_cycle_id: str | None = None,
        risk_snapshot_id: str | None = None,
        preview_id: str | None = None,
        confirmation_hash: str | None = None,
    ) -> OrderIntentRecord:
        local_symbol = getattr(self.contract, "local_symbol", None)
        return OrderIntentRecord(
            intent_id=self.intent_id,
            idempotency_key=self.idempotency_key(day_bucket=day_bucket),
            account_fingerprint=account_fingerprint(self.account_id),
            environment=environment.value,
            product_family=self.product_family,
            wheel_cycle_id=wheel_cycle_id,
            symbol=self.symbol,
            con_id=getattr(self.contract, "con_id", None),
            local_symbol=local_symbol,
            action=self.side,
            open_close_effect=self.open_close_effect,
            quantity=self.quantity,
            order_type=self.order_type,
            limit_price=self.limit_price,
            time_in_force=self.time_in_force,
            outside_rth=self.outside_rth,
            quote_snapshot_id=None,
            risk_snapshot_id=risk_snapshot_id,
            preview_id=preview_id,
            confirmation_hash=confirmation_hash,
            order_ref=self.correlation_id,
            status=status,
            created_at=created_at,
            confirmed_at=None,
            submitted_at=None,
            expires_at=expires_at,
        )


def order_summary_hash(intent: WheelOrderIntent, *, risk_decision_id: str) -> str:
    """Server-re-derivable hash binding a confirmation to this exact order.

    Folds the economic terms and the authorizing risk decision id together so a
    typed confirmation cannot be replayed against a different order or a
    different (e.g. expired-and-refreshed) risk evaluation. The client never
    supplies this — the backend recomputes it at both confirm and submit time.
    """

    con_id = getattr(intent.contract, "con_id", 0)
    parts = (
        intent.correlation_id,
        intent.product_family.value,
        intent.symbol,
        str(con_id),
        intent.intent.value,
        intent.side.value,
        canonical_quantity(intent.quantity),
        format(intent.limit_price, "f"),
        intent.time_in_force,
        risk_decision_id,
    )
    payload = "\x1f".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_option_intent(
    *,
    account_id: str,
    intent: OrderIntent,
    contract: OptionContract,
    quantity: int | Decimal,
    limit_price: Decimal,
    correlation_id: str | None = None,
    intent_id: str | None = None,
    outside_rth: bool = False,
) -> WheelOrderIntent:
    return WheelOrderIntent(
        intent_id=intent_id or uuid.uuid4().hex,
        correlation_id=correlation_id or new_correlation_id(),
        account_id=account_id,
        product_family=ProductFamily.OPTION,
        intent=intent,
        contract=contract,
        quantity=Decimal(quantity),
        limit_price=limit_price,
        outside_rth=outside_rth,
    )


def build_stock_intent(
    *,
    account_id: str,
    intent: OrderIntent,
    contract: UnderlyingContract,
    quantity: int | Decimal,
    limit_price: Decimal,
    correlation_id: str | None = None,
    intent_id: str | None = None,
) -> WheelOrderIntent:
    return WheelOrderIntent(
        intent_id=intent_id or uuid.uuid4().hex,
        correlation_id=correlation_id or new_correlation_id(),
        account_id=account_id,
        product_family=ProductFamily.STOCK,
        intent=intent,
        contract=contract,
        quantity=Decimal(quantity),
        limit_price=limit_price,
        outside_rth=False,
    )


def build_crypto_intent(
    *,
    account_id: str,
    intent: OrderIntent,
    contract: CryptoContract,
    quantity: int | Decimal,
    limit_price: Decimal,
    time_in_force: Literal["DAY", "IOC"] = "DAY",
    correlation_id: str | None = None,
    intent_id: str | None = None,
) -> WheelOrderIntent:
    """Build a spot-crypto intent (ADR-0010): fractional Decimal quantity, PAXOS venue.

    ``time_in_force`` is family-conditional — DAY or the owner-verified crypto TIF;
    the risk engine's family-aware ``limit_only`` check re-asserts the configured
    value, so passing an unpermitted TIF here still fails closed downstream.
    """

    return WheelOrderIntent(
        intent_id=intent_id or uuid.uuid4().hex,
        correlation_id=correlation_id or new_correlation_id(),
        account_id=account_id,
        product_family=ProductFamily.CRYPTO,
        intent=intent,
        contract=contract,
        quantity=Decimal(quantity),
        limit_price=limit_price,
        time_in_force=time_in_force,
        outside_rth=False,
    )
