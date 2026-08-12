"""Shared, immutable value objects for deterministic strategy calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from .errors import DomainError

type DecimalLike = Decimal | int | str | float


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


@dataclass(frozen=True, slots=True)
class DailyBar:
    """A SPY regular-session daily close and its completion status."""

    session_date: date
    close: Decimal
    completed: bool = True

    def __post_init__(self) -> None:
        close = decimal_value(self.close, field="close")
        if close <= 0:
            raise DomainError("close must be positive")
        object.__setattr__(self, "close", close)
