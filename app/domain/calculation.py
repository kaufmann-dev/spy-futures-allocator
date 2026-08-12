"""Pure ARL-5 leverage calculations over completed SPY daily bars."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from .errors import CalculationUnavailable, DomainError
from .types import DailyBar, DecimalLike, decimal_value

ZERO = Decimal("0")
ONE = Decimal("1")
MAX_TARGET = Decimal("4.85")
MIN_VOLATILITY = Decimal("0.08")
ANNUALIZATION_DAYS = Decimal("252")


@dataclass(frozen=True, slots=True)
class LeverageMetrics:
    spy_close: Decimal
    sma200: Decimal
    distance_above_sma200: Decimal
    daily_return: Decimal
    rv20: Decimal
    prior_20_session_high: Decimal
    dd20: Decimal


@dataclass(frozen=True, slots=True)
class LeverageResult:
    metrics: LeverageMetrics
    base: Decimal
    low_vol_score: Decimal
    bonus: Decimal
    volatility_cap: Decimal
    raw_target: Decimal


def _completed_closes(bars: Iterable[DailyBar]) -> list[Decimal]:
    materialized = list(bars)
    if not materialized:
        raise CalculationUnavailable("SPY history is missing")

    previous_date = None
    completed: list[Decimal] = []
    for bar in materialized:
        if not isinstance(bar, DailyBar):
            raise CalculationUnavailable("SPY history contains an invalid bar")
        if previous_date is not None and bar.session_date <= previous_date:
            raise CalculationUnavailable(
                "SPY bars must have unique, strictly increasing session dates"
            )
        previous_date = bar.session_date
        if bar.completed:
            completed.append(bar.close)

    if len(completed) < 200:
        raise CalculationUnavailable("at least 200 completed SPY bars are required")
    return completed


def _sample_standard_deviation(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        raise CalculationUnavailable("at least two observations are required")
    mean = sum(values, ZERO) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), ZERO) / Decimal(len(values) - 1)
    return variance.sqrt()


def base_leverage(close: DecimalLike, sma200: DecimalLike) -> Decimal:
    close_value = decimal_value(close, field="close")
    sma_value = decimal_value(sma200, field="sma200")
    if close_value <= 0 or sma_value <= 0:
        raise CalculationUnavailable("close and SMA200 must be positive")

    distance = close_value / sma_value - ONE
    if close_value <= sma_value:
        return ZERO
    if distance < Decimal("0.01"):
        return Decimal("1.00")
    if distance < Decimal("0.02"):
        return Decimal("2.00")
    if distance < Decimal("0.035"):
        return Decimal("3.00")
    if distance < Decimal("0.05"):
        return Decimal("4.00")
    return MAX_TARGET


def low_volatility_score(rv20: DecimalLike) -> Decimal:
    rv = decimal_value(rv20, field="RV20")
    if rv < 0:
        raise CalculationUnavailable("RV20 cannot be negative")
    score = (Decimal("0.20") - rv) / Decimal("0.12")
    return min(ONE, max(ZERO, score))


def leverage_from_metrics(metrics: LeverageMetrics) -> LeverageResult:
    """Apply the specified ladder, bonus, and volatility cap."""

    base = base_leverage(metrics.spy_close, metrics.sma200)
    low_vol_score = low_volatility_score(metrics.rv20)
    bonus = low_vol_score if base > ZERO and metrics.dd20 >= Decimal("-0.01") else ZERO
    volatility_cap = ONE / max(metrics.rv20, MIN_VOLATILITY)
    raw_target = min(MAX_TARGET, base + bonus, volatility_cap)
    return LeverageResult(
        metrics=metrics,
        base=base,
        low_vol_score=low_vol_score,
        bonus=bonus,
        volatility_cap=volatility_cap,
        raw_target=raw_target,
    )


def calculate_leverage(bars: Iterable[DailyBar]) -> LeverageResult:
    """Calculate the deterministic target, ignoring any incomplete current bar."""

    try:
        closes = _completed_closes(bars)
        with localcontext() as context:
            context.prec = 40
            latest = closes[-1]
            sma200 = sum(closes[-200:], ZERO) / Decimal(200)
            prior_close = closes[-2]
            daily_return = latest / prior_close - ONE
            latest_returns = [
                closes[index] / closes[index - 1] - ONE
                for index in range(len(closes) - 20, len(closes))
            ]
            rv20 = _sample_standard_deviation(latest_returns) * ANNUALIZATION_DAYS.sqrt()
            prior_high = max(closes[-21:-1])
            dd20 = latest / prior_high - ONE
            metrics = LeverageMetrics(
                spy_close=latest,
                sma200=sma200,
                distance_above_sma200=latest / sma200 - ONE,
                daily_return=daily_return,
                rv20=rv20,
                prior_20_session_high=prior_high,
                dd20=dd20,
            )
            return leverage_from_metrics(metrics)
    except CalculationUnavailable:
        raise
    except DomainError as exc:
        raise CalculationUnavailable(str(exc)) from exc
    except (ArithmeticError, ValueError) as exc:
        raise CalculationUnavailable("SPY metrics could not be calculated") from exc
