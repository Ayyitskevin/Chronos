"""Hard process-safety limits shared across candidate request boundaries."""

from decimal import Decimal

MAX_CANDIDATE_EXPIRATIONS = 8
MAX_CANDIDATE_STRIKES_PER_EXPIRATION = 20
MAX_CANDIDATE_REQUEST_CONTRACTS = 80
MAX_OPTION_CHAIN_ROWS = 32
MAX_OPTION_CHAIN_EXPIRATIONS_PER_ROW = 512
MAX_OPTION_CHAIN_STRIKES_PER_ROW = 4096
MAX_OPTION_MARKET_RULE_INCREMENTS = 256
MAX_OPTION_SELECTION_RECEIPT_BYTES = 1_000_000
# The durable outer envelope is encoded with ASCII JSON.  A UTF-8 receipt byte
# can expand to at most three JSON characters, while its duplicated identifiers
# add less than 1 KiB.  Four times the receipt ceiling is therefore a
# conservative shared read bound for both the supervisor and terminal.
MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS = MAX_OPTION_SELECTION_RECEIPT_BYTES * 4
MAX_DURABLE_SEQUENCE_TEXT_CHARS = 20
MAX_DURABLE_TIMESTAMP_TEXT_CHARS = 64
MAX_DURABLE_KIND_TEXT_CHARS = 64
MAX_DURABLE_HASH_TEXT_CHARS = 64
MAX_OPTION_SELECTION_WEIGHT = Decimal("100")
# Composite wall-clock bound for the seven-stage read-only acquisition.  This
# exceeds the current worst case of bounded pacing retries, four sequential
# qualification batches, two quote-batch waves, and cancellation cleanup; the
# connection manager's 30-second single-call default would otherwise cancel a
# healthy bounded acquisition mid-flight and discard already-read evidence.
MAX_OPTION_SELECTION_ACQUISITION_SECONDS = 600.0
MAX_OPTION_SELECTION_DECIMAL_DIGITS = 32
MIN_OPTION_SELECTION_DECIMAL_EXPONENT = -18
MAX_OPTION_SELECTION_DECIMAL_EXPONENT = 18
MAX_OPTION_SELECTION_DERIVED_DECIMAL_DIGITS = 64
MIN_OPTION_SELECTION_DERIVED_DECIMAL_EXPONENT = -128
MAX_OPTION_SELECTION_DERIVED_DECIMAL_EXPONENT = 128
MAX_OPTION_SELECTION_TEXT_CHARS = 4096
MAX_OPTION_SELECTION_POLICY_CODES = 80
MAX_OPTION_SELECTION_OTHER_ASSETS = 32
MAX_OPTION_SELECTION_REJECTIONS = 128
MAX_BROKER_CODE_CHARS = 32
MAX_BROKER_LOCAL_SYMBOL_CHARS = 128
MAX_BROKER_SESSION_CHARS = 4096
MAX_BROKER_TIME_ZONE_CHARS = 128
MAX_BROKER_EVIDENCE_SOURCE_CHARS = 128
MAX_OPTION_DELIVERABLE_ASSET_CHARS = 128


def validate_option_selection_decimal(
    value: Decimal,
    label: str,
    *,
    max_digits: int = MAX_OPTION_SELECTION_DECIMAL_DIGITS,
    min_exponent: int = MIN_OPTION_SELECTION_DECIMAL_EXPONENT,
    max_exponent: int = MAX_OPTION_SELECTION_DECIMAL_EXPONENT,
) -> Decimal:
    """Keep fixed-point canonical rendering inside a small structural envelope.

    ``Decimal('1E-1000000')`` is finite and numerically small, but rendering it
    as canonical fixed-point JSON allocates roughly one megabyte.  Validate the
    coefficient and exponent tuple directly so request and policy values cannot
    defeat the durable receipt bound before the selector can emit a refusal.
    Trailing coefficient zeroes are ignored because canonical JSON removes them.
    """

    if not value.is_finite():
        raise ValueError(f"{label} must be finite")
    if value == 0:
        return value
    parts = value.as_tuple()
    exponent = parts.exponent
    if not isinstance(exponent, int):
        raise ValueError(f"{label} must have a finite integer exponent")
    trailing_zeroes = 0
    for digit in reversed(parts.digits):
        if digit != 0:
            break
        trailing_zeroes += 1
    significant_digits = len(parts.digits) - trailing_zeroes
    exponent += trailing_zeroes
    if significant_digits > max_digits:
        raise ValueError(f"{label} exceeds {max_digits} significant digits")
    if not min_exponent <= exponent <= max_exponent:
        raise ValueError(f"{label} exponent must be between {min_exponent} and {max_exponent}")
    return value
