"""Shared, immutable value objects for deterministic strategy calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import IntEnum

from .errors import DomainError

type DecimalLike = Decimal | int | str | float


class Weekday(IntEnum):
    """ISO-style weekday numbers used by :class:`datetime.date`."""

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4


def decimal_value(value: DecimalLike, *, field: str) -> Decimal:
    """Convert an external numeric value without introducing binary-float noise."""

    if isinstance(value, bool):
        raise DomainError(f"{field} must be numeric")
    try:
        converted = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DomainError(f"{field} must be numeric") from exc
    if not converted.is_finite():
        raise DomainError(f"{field} must be finite")
    return converted


def _positive_integer(value: object, *, field: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainError(f"{field} must be an integer")
    if value < minimum:
        raise DomainError(f"{field} must be at least {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Canonical parameters for the SPY-signaled futures strategy."""

    fast_sma_sessions: int = 100
    slow_sma_sessions: int = 500
    rebound_fraction: Decimal = Decimal("0.25")
    rv_window_sessions: int = 20
    annualization_sessions: int = 252
    rv_threshold: Decimal = Decimal("0.25")
    normal_exposure: Decimal = Decimal("1.60")
    high_vol_exposure: Decimal = Decimal("0.60")
    review_weekdays: tuple[int, ...] = (Weekday.MONDAY, Weekday.THURSDAY)

    def __post_init__(self) -> None:
        fast = _positive_integer(self.fast_sma_sessions, field="fast_sma_sessions")
        slow = _positive_integer(self.slow_sma_sessions, field="slow_sma_sessions")
        rv_window = _positive_integer(
            self.rv_window_sessions, field="rv_window_sessions", minimum=2
        )
        annualization = _positive_integer(
            self.annualization_sessions, field="annualization_sessions"
        )
        if fast >= slow:
            raise DomainError("fast_sma_sessions must be less than slow_sma_sessions")

        decimal_fields = {
            "rebound_fraction": self.rebound_fraction,
            "rv_threshold": self.rv_threshold,
            "normal_exposure": self.normal_exposure,
            "high_vol_exposure": self.high_vol_exposure,
        }
        normalized: dict[str, Decimal] = {}
        for field, value in decimal_fields.items():
            converted = decimal_value(value, field=field)
            if converted <= 0:
                raise DomainError(f"{field} must be positive")
            normalized[field] = converted
        if normalized["high_vol_exposure"] >= normalized["normal_exposure"]:
            raise DomainError("high_vol_exposure must be less than normal_exposure")

        try:
            weekdays = tuple(self.review_weekdays)
        except TypeError as exc:
            raise DomainError("review_weekdays must be an iterable of weekdays") from exc
        if not weekdays:
            raise DomainError("at least one review weekday is required")
        if any(isinstance(day, bool) or not isinstance(day, int) for day in weekdays):
            raise DomainError("review weekdays must be integers")
        if any(day < Weekday.MONDAY or day > Weekday.FRIDAY for day in weekdays):
            raise DomainError("review weekdays must be between Monday and Friday")

        object.__setattr__(self, "fast_sma_sessions", fast)
        object.__setattr__(self, "slow_sma_sessions", slow)
        object.__setattr__(self, "rv_window_sessions", rv_window)
        object.__setattr__(self, "annualization_sessions", annualization)
        for field, value in normalized.items():
            object.__setattr__(self, field, value)
        object.__setattr__(self, "review_weekdays", tuple(sorted(set(weekdays))))

    @property
    def minimum_history_sessions(self) -> int:
        """Completed sessions required for both SMAs and realized volatility."""

        return max(self.slow_sma_sessions, self.rv_window_sessions + 1)

    def reviews_on(self, session_date: date) -> bool:
        """Return whether a real session on this weekday is an exposure review."""

        return session_date.weekday() in self.review_weekdays


@dataclass(frozen=True, slots=True)
class DailyBar:
    """A dividend-adjusted SPY regular-session close and completion status."""

    session_date: date
    close: Decimal
    completed: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, date) or type(self.session_date) is not date:
            raise DomainError("session_date must be a date")
        if not isinstance(self.completed, bool):
            raise DomainError("completed must be boolean")
        close = decimal_value(self.close, field="close")
        if close <= 0:
            raise DomainError("close must be positive")
        object.__setattr__(self, "close", close)
