"""Environment-backed, fail-closed Chronos settings."""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BeforeValidator, Field, PositiveInt, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from chronos.config.limits import (
    MAX_CANDIDATE_EXPIRATIONS,
    MAX_CANDIDATE_REQUEST_CONTRACTS,
    MAX_CANDIDATE_STRIKES_PER_EXPIRATION,
)
from chronos.domain.accounts import is_live_account_id
from chronos.domain.enums import BrokerAdapter, BrokerMode, DemoProfile, IBEnvironment


def _parse_symbol_allowlist(value: object) -> object:
    """Accept a comma-separated list, a JSON array, or an already-parsed sequence.

    **This validator's string branch was unreachable from the environment until
    2026-08-14.** ``pydantic-settings`` JSON-decodes any field whose type is
    "complex" (a tuple here) *before* validators run, so every documented value
    of ``IB_ACCOUNT_ALLOWLIST`` / ``CRYPTO_ALLOWLIST`` / ``SYMBOL_ALLOWLIST``
    raised ``SettingsError`` at import: the bare ``IB_ACCOUNT_ALLOWLIST=`` that
    ``.env.example`` shipped, the comma form this function exists to parse, and
    anything else that was not already JSON. Only a JSON array ever worked, and
    nothing said so. ``cp .env.example .env`` — the README's own setup step —
    therefore could not boot.

    The tests did not catch it because they construct ``Settings(...)`` directly,
    which bypasses the settings sources entirely and runs this validator on a
    real string. A control exercised only on a path production never takes is
    the R-24..R-27 shape, here applied to configuration parsing rather than to a
    gate.

    ``NoDecode`` on the annotation below now hands us the raw string, so this
    function is the single parser for every source. It accepts both spellings on
    purpose: the comma form is what the docs promise, and the JSON form is what
    anyone who diagnosed the failure will have switched to — silently
    reinterpreting their working ``["DU123"]`` as one garbage symbol would be a
    worse outcome than the bug being fixed.

    Empty stays empty, which is **deny**: an empty account allowlist makes
    ``live_transmission_possible`` false and is reported as a problem by
    ``live_configuration_problems``. Nothing here can widen a scope — the parse
    result still faces the duplicate and alphanumeric validators, the
    account-match check, and the whole ADR-0009 conjunction.
    """

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            # A JSON array: what pydantic-settings used to decode for us, and so
            # the only spelling that worked before this fix. Parsed here to keep
            # every previously-working configuration working unchanged.
            try:
                decoded = json.loads(text)
            except ValueError as error:
                raise ValueError(
                    "an allowlist that opens with '[' must be a valid JSON array of "
                    "strings; use the comma-separated form (A,B,C) if that was not "
                    "the intent"
                ) from error
            if not isinstance(decoded, list):
                raise ValueError("a JSON allowlist must be an array of strings")
            return tuple(str(entry).strip().upper() for entry in decoded if str(entry).strip())
        return tuple(part.strip().upper() for part in text.split(",") if part.strip())
    return value


#: ``NoDecode`` is load-bearing, not decoration: without it pydantic-settings
#: JSON-decodes the raw value before :func:`_parse_symbol_allowlist` ever sees
#: it, which is what made every documented allowlist value fail to parse.
SymbolAllowlist = Annotated[tuple[str, ...], NoDecode, BeforeValidator(_parse_symbol_allowlist)]


class Settings(BaseSettings):
    """Validated settings loaded from environment variables or a local `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # ADR-0009: branch selection (paper vs live) is derived from settings, so
        # process-lifetime immutability is enforced by the type, not convention.
        frozen=True,
    )

    broker_mode: BrokerMode = BrokerMode.DEMO
    broker_adapter: BrokerAdapter = BrokerAdapter.OFFICIAL_IBKR
    demo_profile: DemoProfile = DemoProfile.SAFETY_CASES
    ib_environment: IBEnvironment = IBEnvironment.PAPER
    ib_host: str = "127.0.0.1"
    ib_port: PositiveInt = 7497
    ib_client_id: Annotated[int, Field(ge=0)] = 17
    # Dedicated client id for the read-only historical-data process (ADR-0011 §1).
    # ge=1 (not ge=0): client id 0 is TWS/Gateway's master id, wrong for a data plane.
    # Must differ from ib_client_id — TWS rejects two live connections sharing an id.
    ib_data_client_id: Annotated[int, Field(ge=1)] = 18
    # Options forward-capture bounds (ADR-0012 §3). The capture window is recorded in
    # every snapshot, so anything outside it is absent *by policy*, not missing data.
    option_capture_expiry_horizon_days: Annotated[int, Field(ge=1)] = 120
    option_capture_strike_window_pct: Annotated[float, Field(gt=0, le=1)] = 0.20
    # Holdout-unlock guardian (ADR-0013 §6/§8). The unlock phrase is a module constant,
    # never a setting, so it is never serialized or logged.
    holdout_unlock_ttl_minutes: Annotated[int, Field(gt=0, le=120)] = 15
    holdout_sessions_per_unlock: Annotated[int, Field(ge=1)] = 20
    holdout_max_outstanding_unlocks: Annotated[int, Field(ge=0)] = 2
    # Walk-forward + sample-honest verdict defaults (ADR-0014 §2/§4). The out-of-sample
    # window and warm-up prefix are in bars; the trade floor is the C4 minimum below which
    # the verdict is a blocking INSUFFICIENT_EVIDENCE regardless of the point statistic.
    walkforward_test_window_bars: Annotated[int, Field(ge=2)] = 63
    walkforward_warmup_bars: Annotated[int, Field(ge=1)] = 252
    walkforward_min_trades: Annotated[int, Field(ge=1)] = 20
    ib_account_id: str = ""

    allow_order_transmit: bool = False
    allow_live_trading: bool = False
    allow_outside_rth: bool = False

    # Live-wheel configuration surface (M1) — since Milestone 7 the live flag
    # is honored under the strict ADR-0009 conjunction validated below, and
    # `live_transmission_possible` re-derives that conjunction at every read.
    ib_account_allowlist: SymbolAllowlist = ()
    enable_paper_trading: bool = True
    require_live_arming: bool = True
    live_arm_ttl_minutes: Annotated[int, Field(gt=0, le=120)] = 15
    require_typed_confirmation: bool = True
    order_confirmation_ttl_seconds: Annotated[int, Field(gt=0, le=300)] = 20
    max_open_short_option_contracts: Annotated[int, Field(ge=0)] = 5
    max_opening_orders_per_day: Annotated[int, Field(ge=0)] = 3
    max_gross_assignment_usd: Annotated[Decimal, Field(ge=0)] = Decimal("25000")
    min_cash_buffer_usd: Annotated[Decimal, Field(ge=0)] = Decimal("5000")
    min_cash_buffer_pct: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.10")
    min_excess_liquidity_usd: Annotated[Decimal, Field(ge=0)] = Decimal("10000")
    max_session_drawdown_usd: Annotated[Decimal, Field(ge=0)] = Decimal("1000")
    max_session_drawdown_pct: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.02")

    # Product families beyond options (owner-directed scope, plan §6b).
    # An empty crypto allowlist keeps the crypto family entirely disabled.
    crypto_allowlist: SymbolAllowlist = ()
    max_crypto_allocation_pct: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.10")
    max_crypto_notional_per_order_usd: Annotated[Decimal, Field(ge=0)] = Decimal("1000")
    # ADR-0010 §3: family-conditional TIF. IBKR's accepted Paxos-crypto TIF set
    # is an owner gateway-verification item; DAY is the safe default and the
    # allowed set stays narrow. Options/stocks are always DAY (unaffected).
    crypto_time_in_force: Literal["DAY", "IOC"] = "DAY"

    # Local backend service (FastAPI); loopback-only by design.
    backend_host: str = "127.0.0.1"
    backend_port: Annotated[int, Field(gt=0, lt=65536)] = 8765
    backend_token_file: Path = Path("data/backend_api_token")

    # Live safety layer (Milestone 6). Durable, atomic-write flag files owned by
    # the live-Wheel path; the kill switch defaults DISENGAGED (a fresh deploy
    # trades subject to the other gates) but fails closed on a corrupt file.
    live_kill_switch_file: Path = Path("data/live_kill_switch.json")
    session_baseline_file: Path = Path("data/session_baseline.json")

    # Bounded periodic reconciliation (ADR-0020 / D-20). These are owner-frozen
    # operational thresholds, not tuning knobs: the cadence is set by what can
    # change the book without an order this system placed (a fill on a resting
    # limit; overnight assignment), and bounded on the other side by the shared
    # pacing budget, where headroom is a safety property because rate limit spent
    # watching is rate limit unavailable to cancel. Widening them to make a
    # symptom disappear is exactly the move the change-control rules forbid.
    #
    # The age is deliberately larger than one interval and smaller than two, so
    # a single missed cycle is survivable and two consecutive misses fail closed
    # without any failure detector. It is also what makes readiness unable to
    # cross a session open (ADR-0020 §4).
    reconciliation_interval_active_seconds: Annotated[float, Field(gt=0)] = 120.0
    reconciliation_interval_idle_seconds: Annotated[float, Field(gt=0)] = 240.0
    reconciliation_interval_closed_seconds: Annotated[float, Field(gt=0)] = 1800.0
    reconciliation_max_evidence_age_seconds: Annotated[float, Field(gt=0)] = 300.0

    # Autonomy runtime (ADR-0017, owner-directed persistent authority). The
    # mandate file is the owner's standing grant: authored once, validated on
    # every boot, auto-activated when present. An empty path means no autonomy
    # runtime starts — absence of the grant is absence of the authority, so a
    # fresh checkout still boots with the model plane inert.
    autonomy_mandate_file: Path | None = None
    # The proposer registry (ADR-0023): the owner's record of who may propose
    # and as whom. Unset (the default) preserves the pre-ADR-0023 behavior —
    # the local API token authenticates proposals and the static ingress
    # identity is stamped. Set, it flips the proposal route to proposer-only
    # credentials and provenance to credential-derived identity; an invalid or
    # unreadable file means proposals refuse, never that identity is guessed.
    autonomy_proposers_file: Path | None = None
    # The per-job evidence protocol (ADR-0028 Option C). Unset (the default) is
    # the pre-ADR-0028 posture byte-for-byte: every proposal cites the
    # placeholder bundle, and admission check 9 compares that constant against
    # itself. Set, every proposal must cite an evidence bundle this backend
    # issued to that proposer and that has not expired against the drain's
    # clock — and check 9 gains the payload-side half it has never had.
    #
    # Set WITHOUT a proposer registry refuses every proposal rather than falling
    # back: a bundle is issued *to* a credential, and with no registry there is
    # no author to issue to. That combination is a configuration error the owner
    # must see, never a quiet return to anonymous proposing.
    autonomy_evidence_bundles: bool = False
    # How long an issued bundle stays citable, judged at the drain. 300 s is a
    # disclosed judgment, not a derived number (the MARKET_PROTECTION_COLLAR
    # precedent): it must exceed worst-case gather -> model call -> POST -> queue
    # wait -> drain latency, and a TTL below a worker's think time refuses
    # everything — which is the safe direction and a visible failure rather than
    # a silent one. The ceiling is likewise a judgment: evidence an hour old is
    # stale by any reading of an intraday equity decision, so the type refuses to
    # express a longer window. An unparsable or out-of-range value fails
    # validation and the process refuses to start.
    autonomy_evidence_ttl_seconds: Annotated[float, Field(gt=0, le=3600)] = 300.0
    autonomy_alert_file: Path = Path("data/owner_alerts.jsonl")
    autonomy_idle_interval_seconds: Annotated[float, Field(gt=0)] = 60.0
    autonomy_min_interval_seconds: Annotated[float, Field(gt=0)] = 5.0
    autonomy_market_timezone: str = "America/New_York"

    symbol_allowlist: SymbolAllowlist = ("AAPL", "MSFT", "SPY")
    target_abs_delta: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.30")
    min_abs_delta: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.20")
    max_abs_delta: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.35")
    min_dte: Annotated[int, Field(ge=0)] = 7
    target_dte: Annotated[int, Field(ge=0)] = 21
    max_dte: Annotated[int, Field(ge=0)] = 45
    max_expirations: Annotated[int, Field(gt=0, le=MAX_CANDIDATE_EXPIRATIONS)] = 6
    max_strikes_per_expiration: Annotated[
        int, Field(gt=0, le=MAX_CANDIDATE_STRIKES_PER_EXPIRATION)
    ] = 12
    min_option_volume: Annotated[int, Field(ge=0)] = 10
    min_open_interest: Annotated[int, Field(ge=0)] = 100
    max_relative_spread: Annotated[Decimal, Field(gt=0)] = Decimal("0.20")
    max_quote_age_seconds: PositiveInt = 5
    market_timezone: str = "America/New_York"
    max_contracts_per_order: PositiveInt = 1
    max_symbol_allocation_pct: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("0.25")
    max_total_wheel_allocation_pct: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("0.60")

    delta_weight: Annotated[Decimal, Field(ge=0)] = Decimal("0.45")
    spread_weight: Annotated[Decimal, Field(ge=0)] = Decimal("0.30")
    dte_weight: Annotated[Decimal, Field(ge=0)] = Decimal("0.15")
    liquidity_weight: Annotated[Decimal, Field(ge=0)] = Decimal("0.10")

    assignment_near_zero_extrinsic: Annotated[Decimal, Field(ge=0)] = Decimal("0.05")
    assignment_meaningful_extrinsic: Annotated[Decimal, Field(ge=0)] = Decimal("0.10")
    assignment_elevated_abs_delta: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("0.50")
    assignment_high_dte: Annotated[int, Field(ge=0)] = 3
    assignment_elevated_dte: Annotated[int, Field(ge=0)] = 5
    assignment_ex_dividend_window_days: Annotated[int, Field(ge=0)] = 5

    database_url: str = "sqlite:///data/chronos.db"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_file: Path = Path("logs/chronos.log")

    @model_validator(mode="after")
    def validate_reconciliation_cadence(self) -> Settings:
        """The evidence age must outlast the refresh that keeps it warm (ADR-0020 §3).

        If the age were shorter than an in-session interval, readiness would expire
        before its own scheduled refresh with nothing failing — a permanently
        blocked submission path that looks like a safety property and is actually a
        misconfiguration. Refusing at startup is how that stays diagnosable.

        The closed-market interval is deliberately exempt: while the market is
        closed, readiness expiring and staying expired is the correct state.
        """

        age = self.reconciliation_max_evidence_age_seconds
        for name, interval in (
            ("reconciliation_interval_active_seconds", self.reconciliation_interval_active_seconds),
            ("reconciliation_interval_idle_seconds", self.reconciliation_interval_idle_seconds),
        ):
            if interval >= age:
                raise ValueError(
                    f"{name}={interval}s must be shorter than "
                    f"reconciliation_max_evidence_age_seconds={age}s, or reconciliation "
                    "readiness expires before the refresh that would renew it"
                )
        return self

    @model_validator(mode="after")
    def validate_safety_and_ranges(self) -> Settings:
        if self.allow_live_trading:
            # ADR-0009 §2: live capability is a strict conjunction, never one flag.
            # Every unmet conjunct is named so a misconfiguration is diagnosable
            # without weakening the refusal. The live gate stack (arming, typed
            # confirmation, kill switch, drawdown breaker, ten-gate walk) applies
            # at runtime ON TOP of this load-time validation.
            problems: list[str] = []
            if self.broker_mode is not BrokerMode.IBKR:
                problems.append("BROKER_MODE must be 'ibkr'")
            if self.broker_adapter is not BrokerAdapter.OFFICIAL_IBKR:
                problems.append(
                    "BROKER_ADAPTER must be 'official_ibkr' (the only adapter with a "
                    "validated live order path)"
                )
            if self.ib_environment is not IBEnvironment.LIVE:
                problems.append("IB_ENVIRONMENT must be 'live'")
            if not self.allow_order_transmit:
                problems.append("ALLOW_ORDER_TRANSMIT must be true (transmission master switch)")
            if not is_live_account_id(self.ib_account_id):
                problems.append(
                    "IB_ACCOUNT_ID must match the IBKR live account pattern (U + digits)"
                )
            if not self.ib_account_allowlist:
                problems.append("IB_ACCOUNT_ALLOWLIST must not be empty")
            elif self.ib_account_id not in self.ib_account_allowlist:
                problems.append("IB_ACCOUNT_ID must be on IB_ACCOUNT_ALLOWLIST")
            if not self.require_live_arming:
                problems.append("REQUIRE_LIVE_ARMING must remain true (MVP live model)")
            if not self.require_typed_confirmation:
                problems.append("REQUIRE_TYPED_CONFIRMATION must remain true (MVP live model)")
            if problems:
                raise ValueError(
                    "ALLOW_LIVE_TRADING=true requires the full live conjunction "
                    "(ADR-0009, docs/LIVE_WHEEL_GAME_PLAN.md): " + "; ".join(problems)
                )
        if not self.symbol_allowlist:
            raise ValueError("SYMBOL_ALLOWLIST must contain at least one symbol")
        if len(set(self.symbol_allowlist)) != len(self.symbol_allowlist):
            raise ValueError("SYMBOL_ALLOWLIST must not contain duplicates")
        if any(not symbol.isalnum() for symbol in self.symbol_allowlist):
            raise ValueError("SYMBOL_ALLOWLIST entries must be alphanumeric")
        if not self.min_abs_delta <= self.target_abs_delta <= self.max_abs_delta:
            raise ValueError("TARGET_ABS_DELTA must be between MIN_ABS_DELTA and MAX_ABS_DELTA")
        if not self.min_dte <= self.target_dte <= self.max_dte:
            raise ValueError("TARGET_DTE must be between MIN_DTE and MAX_DTE")
        if self.max_expirations * self.max_strikes_per_expiration > MAX_CANDIDATE_REQUEST_CONTRACTS:
            raise ValueError(
                "MAX_EXPIRATIONS * MAX_STRIKES_PER_EXPIRATION must not exceed "
                f"{MAX_CANDIDATE_REQUEST_CONTRACTS}"
            )
        if (
            self.ib_environment is IBEnvironment.LIVE
            and self.allow_order_transmit
            and not self.allow_live_trading
        ):
            raise ValueError(
                "IB_ENVIRONMENT=live with ALLOW_ORDER_TRANSMIT=true is ambiguous without "
                "ALLOW_LIVE_TRADING=true (plus the full ADR-0009 live conjunction); "
                "refusing rather than guess intent"
            )
        if (
            self.broker_mode is BrokerMode.IBKR
            and self.ib_environment is IBEnvironment.PAPER
            and self.allow_order_transmit
            and not self.ib_account_id.strip()
        ):
            raise ValueError("IB_ACCOUNT_ID is required before paper order transmission")
        weight_total = (
            self.delta_weight + self.spread_weight + self.dte_weight + self.liquidity_weight
        )
        if weight_total <= 0:
            raise ValueError("At least one resolver weight must be positive")
        if self.assignment_near_zero_extrinsic > self.assignment_meaningful_extrinsic:
            raise ValueError(
                "ASSIGNMENT_NEAR_ZERO_EXTRINSIC must not exceed ASSIGNMENT_MEANINGFUL_EXTRINSIC"
            )
        if self.assignment_high_dte > self.assignment_elevated_dte:
            raise ValueError("ASSIGNMENT_HIGH_DTE must not exceed ASSIGNMENT_ELEVATED_DTE")
        try:
            ZoneInfo(self.market_timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("MARKET_TIMEZONE must name an installed IANA timezone") from error
        if len(set(self.ib_account_allowlist)) != len(self.ib_account_allowlist):
            raise ValueError("IB_ACCOUNT_ALLOWLIST must not contain duplicates")
        if any(not entry.isalnum() for entry in self.ib_account_allowlist):
            raise ValueError("IB_ACCOUNT_ALLOWLIST entries must be alphanumeric")
        if len(set(self.crypto_allowlist)) != len(self.crypto_allowlist):
            raise ValueError("CRYPTO_ALLOWLIST must not contain duplicates")
        if any(not symbol.isalnum() for symbol in self.crypto_allowlist):
            raise ValueError("CRYPTO_ALLOWLIST entries must be alphanumeric")
        if self.backend_host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                "BACKEND_HOST must be a loopback address; remote exposure of the "
                "order-writing backend is out of scope by design"
            )
        if self.ib_data_client_id == self.ib_client_id:
            raise ValueError(
                "IB_DATA_CLIENT_ID must differ from IB_CLIENT_ID; the read-only "
                "historical-data process needs its own gateway client id (ADR-0011)"
            )
        return self

    @property
    def transmission_possible(self) -> bool:
        """Whether configuration can enter the paper-order transmission path."""

        return (
            self.broker_mode is BrokerMode.IBKR
            and self.ib_environment is IBEnvironment.PAPER
            and self.allow_order_transmit
            and not self.allow_live_trading
            and bool(self.ib_account_id.strip())
        )

    @property
    def live_transmission_possible(self) -> bool:
        """Whether configuration can enter the LIVE transmission path (ADR-0009).

        Re-derives the ENTIRE live conjunction rather than trusting that the
        load-time validator ran (defense against any validation bypass such as
        ``model_copy(update=...)``). Structurally mutually exclusive with
        :attr:`transmission_possible`: ``ib_environment`` is one enum field, so
        no Settings instance can present both paths as possible.
        """

        return (
            self.broker_mode is BrokerMode.IBKR
            and self.broker_adapter is BrokerAdapter.OFFICIAL_IBKR
            and self.ib_environment is IBEnvironment.LIVE
            and self.allow_order_transmit
            and self.allow_live_trading
            and is_live_account_id(self.ib_account_id)
            and bool(self.ib_account_allowlist)
            and self.ib_account_id in self.ib_account_allowlist
            and self.require_live_arming
            and self.require_typed_confirmation
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one validated settings object per process."""

    return Settings()
