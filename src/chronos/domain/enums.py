"""Enumerations shared across broker, strategy, persistence, and UI layers."""

from enum import StrEnum


class BrokerMode(StrEnum):
    DEMO = "demo"
    IBKR = "ibkr"


class DemoProfile(StrEnum):
    SAFETY_CASES = "safety_cases"
    EMPTY_ACCOUNT = "empty_account"


class IBEnvironment(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class DisplayEnvironment(StrEnum):
    DEMO = "DEMO"
    PAPER = "PAPER"
    LIVE = "LIVE"


class BrokerAdapter(StrEnum):
    """Which broker implementation serves BrokerMode.IBKR."""

    DEMO = "demo"
    OFFICIAL_IBKR = "official_ibkr"
    IB_ASYNC = "ib_async"


class DataQuality(StrEnum):
    LIVE = "LIVE"
    FROZEN = "FROZEN"
    DELAYED = "DELAYED"
    DELAYED_FROZEN = "DELAYED_FROZEN"
    DEMO = "DEMO"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class ConnectionState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"


class SecurityType(StrEnum):
    STOCK = "STK"
    OPTION = "OPT"
    CRYPTO = "CRYPTO"


class OptionRight(StrEnum):
    CALL = "C"
    PUT = "P"


class WheelStage(StrEnum):
    FLAT = "FLAT"
    SHORT_PUT_PENDING = "SHORT_PUT_PENDING"
    SHORT_PUT_OPEN = "SHORT_PUT_OPEN"
    PUT_CLOSE_PENDING = "PUT_CLOSE_PENDING"
    LONG_STOCK = "LONG_STOCK"
    SHORT_CALL_PENDING = "SHORT_CALL_PENDING"
    SHORT_CALL_OPEN = "SHORT_CALL_OPEN"
    CALL_CLOSE_PENDING = "CALL_CLOSE_PENDING"
    CLOSING = "CLOSING"
    ASSIGNMENT_RECONCILING = "ASSIGNMENT_RECONCILING"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class ProductFamily(StrEnum):
    """Order-pipeline product families (docs/LIVE_WHEEL_GAME_PLAN.md §6b)."""

    OPTION = "OPTION"
    STOCK = "STOCK"
    CRYPTO = "CRYPTO"


class ReconciliationStatus(StrEnum):
    PENDING = "PENDING"
    RECONCILED = "RECONCILED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class OrderLifecycle(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    WHAT_IF_PREVIEWED = "WHAT_IF_PREVIEWED"
    USER_CONFIRMED = "USER_CONFIRMED"
    SUBMITTED = "SUBMITTED"
    # A submit call that raised or timed out with no broker order id: the true
    # state is unknown and must be resolved by reconciliation, never by a retry
    # (docs/LIVE_WHEEL_GAME_PLAN.md Milestone 5).
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    # An operator cancel has been requested but the broker has not yet confirmed
    # it; the order is still working and may still fill.
    CANCEL_PENDING = "CANCEL_PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class RiskCheckStatus(StrEnum):
    """Tri-state result of one structured risk check; unknown fails closed."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderIntent(StrEnum):
    OPEN_SHORT_PUT = "OPEN_SHORT_PUT"
    OPEN_COVERED_CALL = "OPEN_COVERED_CALL"
    CLOSE_SHORT_OPTION = "CLOSE_SHORT_OPTION"
    # Stock fold-in (plan §6b): long-only equities through the same pipeline.
    OPEN_LONG_STOCK = "OPEN_LONG_STOCK"
    CLOSE_LONG_STOCK = "CLOSE_LONG_STOCK"


class AssignmentPressure(StrEnum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"


class EligibilityStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"
    NO_TRADE = "NO_TRADE"


class BasisEntryType(StrEnum):
    OPENING_OPTION_PREMIUM = "OPENING_OPTION_PREMIUM"
    CLOSING_OPTION_PREMIUM = "CLOSING_OPTION_PREMIUM"
    COMMISSION_ESTIMATE = "COMMISSION_ESTIMATE"
    COMMISSION_ACTUAL = "COMMISSION_ACTUAL"
    ASSIGNMENT_STOCK_FILL = "ASSIGNMENT_STOCK_FILL"
    CALLED_AWAY_STOCK_FILL = "CALLED_AWAY_STOCK_FILL"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"


class DemoCase(StrEnum):
    FLAT_PUT = "Flat symbol eligible for a put"
    COVERED_CALL = "Stock position eligible for a covered call"
    ACTIVE_SHORT_PUT = "Active short put"
    ACTIVE_SHORT_CALL = "Active covered call"
    STALE_DATA = "Stale-data lock"
    NO_VALID_CANDIDATE = "No valid candidate"
    PARTIAL_FILL_WARNING = "Partial-fill reconciliation warning"
