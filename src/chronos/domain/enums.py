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


class DataQuality(StrEnum):
    LIVE = "LIVE"
    FROZEN = "FROZEN"
    DELAYED = "DELAYED"
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


class OptionRight(StrEnum):
    CALL = "C"
    PUT = "P"


class WheelStage(StrEnum):
    FLAT = "FLAT"
    SHORT_PUT_PENDING = "SHORT_PUT_PENDING"
    SHORT_PUT_OPEN = "SHORT_PUT_OPEN"
    LONG_STOCK = "LONG_STOCK"
    SHORT_CALL_PENDING = "SHORT_CALL_PENDING"
    SHORT_CALL_OPEN = "SHORT_CALL_OPEN"
    CLOSING = "CLOSING"
    MANUAL_REVIEW = "MANUAL_REVIEW"


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
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderIntent(StrEnum):
    OPEN_SHORT_PUT = "OPEN_SHORT_PUT"
    OPEN_COVERED_CALL = "OPEN_COVERED_CALL"
    CLOSE_SHORT_OPTION = "CLOSE_SHORT_OPTION"


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
