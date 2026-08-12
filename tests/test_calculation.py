from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest

from app.domain.calculation import (
    LeverageMetrics,
    base_leverage,
    calculate_leverage,
    leverage_from_metrics,
    low_volatility_score,
)
from app.domain.errors import CalculationUnavailable
from app.domain.types import DailyBar


@pytest.mark.parametrize(
    ("distance", "expected"),
    [
        ("0", "0"),
        ("0.0000001", "1.00"),
        ("0.0099999", "1.00"),
        ("0.01", "2.00"),
        ("0.0199999", "2.00"),
        ("0.02", "3.00"),
        ("0.0349999", "3.00"),
        ("0.035", "4.00"),
        ("0.0499999", "4.00"),
        ("0.05", "4.85"),
    ],
)
def test_base_ladder_boundaries(distance: str, expected: str) -> None:
    sma = Decimal("100")
    close = sma * (Decimal("1") + Decimal(distance))
    assert base_leverage(close, sma) == Decimal(expected)


def test_close_below_sma_has_zero_base() -> None:
    assert base_leverage(Decimal("99.99"), Decimal("100")) == 0


@pytest.mark.parametrize(
    ("rv20", "expected"),
    [("0.0799", "1"), ("0.08", "1"), ("0.14", "0.5"), ("0.20", "0")],
)
def test_low_volatility_score_boundaries(rv20: str, expected: str) -> None:
    assert low_volatility_score(Decimal(rv20)) == Decimal(expected)


def _metrics(*, rv20: str, dd20: str, close: str = "101") -> LeverageMetrics:
    return LeverageMetrics(
        spy_close=Decimal(close),
        sma200=Decimal("100"),
        distance_above_sma200=Decimal(close) / Decimal("100") - 1,
        daily_return=Decimal("0"),
        rv20=Decimal(rv20),
        prior_20_session_high=Decimal("101"),
        dd20=Decimal(dd20),
    )


def test_drawdown_exactly_minus_one_percent_keeps_bonus() -> None:
    result = leverage_from_metrics(_metrics(rv20="0.14", dd20="-0.01"))
    assert result.base == Decimal("2.00")
    assert result.bonus == Decimal("0.5")
    assert result.raw_target == Decimal("2.50")


def test_drawdown_below_minus_one_percent_removes_bonus() -> None:
    result = leverage_from_metrics(_metrics(rv20="0.14", dd20="-0.0100001"))
    assert result.bonus == 0
    assert result.raw_target == Decimal("2.00")


def test_volatility_cap_is_a_hard_cap() -> None:
    result = leverage_from_metrics(_metrics(rv20="0.5", dd20="0", close="106"))
    assert result.volatility_cap == Decimal("2")
    assert result.raw_target == Decimal("2")


def test_calculation_ignores_an_incomplete_current_day() -> None:
    start = date(2023, 1, 1)
    bars = [DailyBar(start + timedelta(days=index), Decimal(100 + index)) for index in range(200)]
    bars.append(DailyBar(start + timedelta(days=200), Decimal("9999"), completed=False))

    result = calculate_leverage(bars)

    assert result.metrics.spy_close == Decimal("299")
    assert result.metrics.sma200 == Decimal("199.5")
    assert result.metrics.prior_20_session_high == Decimal("298")


def test_rv20_uses_sample_standard_deviation_of_twenty_returns() -> None:
    start = date(2023, 1, 1)
    closes = [Decimal("100")]
    for index in range(1, 200):
        change = Decimal("1.01") if index % 2 else Decimal("0.99")
        closes.append(closes[-1] * change)
    bars = [DailyBar(start + timedelta(days=index), close) for index, close in enumerate(closes)]

    result = calculate_leverage(bars)
    with localcontext() as context:
        context.prec = 40
        returns = [closes[index] / closes[index - 1] - 1 for index in range(180, 200)]
        mean = sum(returns) / Decimal(20)
        expected = (sum((value - mean) ** 2 for value in returns) / Decimal(19)).sqrt() * Decimal(
            252
        ).sqrt()

    assert result.metrics.rv20 == expected


def test_calculation_fails_closed_without_required_history() -> None:
    bars = [
        DailyBar(date(2024, 1, 1) + timedelta(days=index), Decimal("100")) for index in range(199)
    ]
    with pytest.raises(CalculationUnavailable, match="at least 200"):
        calculate_leverage(bars)


def test_calculation_fails_closed_for_duplicate_sessions() -> None:
    bars = [
        DailyBar(date(2024, 1, 1) + timedelta(days=index), Decimal("100")) for index in range(200)
    ]
    bars[-1] = DailyBar(bars[-2].session_date, Decimal("101"))
    with pytest.raises(CalculationUnavailable, match="strictly increasing"):
        calculate_leverage(bars)
