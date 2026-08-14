from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal, localcontext

import pytest

from app.domain.calculation import (
    MarketRegime,
    RegimeTransition,
    VolatilityState,
    realized_volatility,
    replay_strategy,
)
from app.domain.errors import CalculationUnavailable, DomainError
from app.domain.types import DailyBar, StrategyConfig, Weekday

SESSIONS = (
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
    date(2024, 1, 9),
    date(2024, 1, 10),
    date(2024, 1, 11),
    date(2024, 1, 12),
)


def config(**overrides: object) -> StrategyConfig:
    values: dict[str, object] = {
        "fast_sma_sessions": 2,
        "slow_sma_sessions": 3,
        "rebound_fraction": Decimal("0.25"),
        "rv_window_sessions": 2,
        "annualization_sessions": 4,
        "rv_threshold": Decimal("0.10"),
        "normal_exposure": Decimal("1.60"),
        "high_vol_exposure": Decimal("0.60"),
        "review_weekdays": (Weekday.MONDAY, Weekday.THURSDAY),
    }
    values.update(overrides)
    return StrategyConfig(**values)  # type: ignore[arg-type]


def bars(*closes: str, completed_last: bool = True) -> list[DailyBar]:
    return [
        DailyBar(
            session_date=SESSIONS[index],
            close=Decimal(close),
            completed=completed_last if index == len(closes) - 1 else True,
        )
        for index, close in enumerate(closes)
    ]


def test_default_configuration_is_canonical_and_immutable() -> None:
    selected = StrategyConfig()

    assert selected.fast_sma_sessions == 100
    assert selected.slow_sma_sessions == 500
    assert selected.rebound_fraction == Decimal("0.25")
    assert selected.rv_window_sessions == 20
    assert selected.annualization_sessions == 252
    assert selected.rv_threshold == Decimal("0.25")
    assert selected.normal_exposure == Decimal("1.60")
    assert selected.high_vol_exposure == Decimal("0.60")
    assert selected.review_weekdays == (Weekday.MONDAY, Weekday.THURSDAY)
    with pytest.raises(FrozenInstanceError):
        selected.normal_exposure = Decimal("2")  # type: ignore[misc]


def test_configuration_normalizes_weekdays_and_has_no_exposure_ceiling() -> None:
    selected = config(
        normal_exposure=Decimal("9.5"),
        high_vol_exposure=Decimal("8"),
        review_weekdays=(3, 0, 3),
    )

    assert selected.normal_exposure == Decimal("9.5")
    assert selected.high_vol_exposure == Decimal("8")
    assert selected.review_weekdays == (0, 3)


def test_configuration_rejects_invalid_parameters() -> None:
    with pytest.raises(DomainError, match="less than slow"):
        config(fast_sma_sessions=3, slow_sma_sessions=3)
    with pytest.raises(DomainError, match="at least 2"):
        config(rv_window_sessions=1)
    with pytest.raises(DomainError, match="finite"):
        config(rv_threshold=Decimal("NaN"))
    with pytest.raises(DomainError, match="positive"):
        config(rebound_fraction=Decimal("0"))
    with pytest.raises(DomainError, match="less than normal"):
        config(high_vol_exposure=Decimal("1.60"))
    with pytest.raises(DomainError, match="at least one"):
        config(review_weekdays=())
    with pytest.raises(DomainError, match="Monday and Friday"):
        config(review_weekdays=(6,))


def test_rv_uses_sample_standard_deviation_and_configured_annualization() -> None:
    closes = [Decimal("100"), Decimal("110"), Decimal("99")]

    result = realized_volatility(closes, window_sessions=2, annualization_sessions=4)

    with localcontext() as context:
        context.prec = 50
        returns = (Decimal("0.1"), Decimal("-0.1"))
        expected = (
            sum(value**2 for value in returns) / Decimal("2") * Decimal("2")
        ).sqrt() * Decimal("4").sqrt()
    assert result == expected


def test_initial_state_is_risk_on_at_sma_equality_and_selects_rv_off_schedule() -> None:
    replay = replay_strategy(bars("100", "100", "100"), config())

    initial = replay.latest
    assert initial.metrics.fast_sma == initial.metrics.slow_sma == Decimal("100")
    assert initial.regime is MarketRegime.RISK_ON
    assert initial.target_exposure == Decimal("1.60")
    assert initial.target_volatility_state is VolatilityState.NORMAL
    assert initial.transition is RegimeTransition.INITIALIZED
    assert initial.exposure_reviewed is False
    assert initial.target_changed is True
    assert initial.exit_armed is True


def test_initial_state_is_risk_off_when_fast_sma_is_below_slow_sma() -> None:
    initial = replay_strategy(bars("120", "110", "100"), config()).latest

    assert initial.regime is MarketRegime.RISK_OFF
    assert initial.target_exposure == 0
    assert initial.post_exit_trough == Decimal("100")
    assert initial.reentry_level == Decimal("125")
    assert initial.exit_armed is False


def test_equality_to_below_is_a_bearish_cross_and_starts_the_trough() -> None:
    replay = replay_strategy(bars("100", "100", "100", "90"), config())

    exited = replay.latest
    assert replay.snapshots[-2].metrics.fast_sma == replay.snapshots[-2].metrics.slow_sma
    assert exited.metrics.fast_sma < exited.metrics.slow_sma
    assert exited.transition is RegimeTransition.BEARISH_EXIT
    assert exited.regime is MarketRegime.RISK_OFF
    assert exited.target_exposure == 0
    assert exited.post_exit_trough == Decimal("90")
    assert exited.reentry_level == Decimal("112.50")


def test_trough_updates_and_exact_twenty_five_percent_rebound_reenters_off_cycle() -> None:
    replay = replay_strategy(bars("100", "100", "100", "90", "80", "100"), config())

    trough_day = replay.snapshots[-2]
    reentry = replay.latest
    assert trough_day.post_exit_trough == Decimal("80")
    assert trough_day.reentry_level == Decimal("100.00")
    assert reentry.session_date.weekday() == Weekday.TUESDAY
    assert reentry.transition is RegimeTransition.REBOUND_REENTRY
    assert reentry.regime is MarketRegime.RISK_ON
    assert reentry.volatility_state is VolatilityState.HIGH
    assert reentry.target_exposure == Decimal("0.60")
    assert reentry.exposure_reviewed is False
    assert reentry.target_changed is True
    assert reentry.exit_armed is False


def test_reentry_rearms_only_after_fast_sma_is_strictly_above_slow_sma() -> None:
    replay = replay_strategy(
        bars("100", "100", "100", "90", "80", "100", "110", "80"), config()
    )

    reentry, rearmed, second_exit = replay.snapshots[-3:]
    assert reentry.exit_armed is False
    assert rearmed.exit_rearmed is True
    assert rearmed.exit_armed is True
    assert second_exit.transition is RegimeTransition.BEARISH_EXIT
    assert second_exit.regime is MarketRegime.RISK_OFF


def test_ordinary_rv_changes_wait_for_configured_review_sessions() -> None:
    replay = replay_strategy(
        bars("100", "100", "100", "120", "120", "120", "120", "120"), config()
    )
    by_date = {snapshot.session_date: snapshot for snapshot in replay.snapshots}

    friday = by_date[date(2024, 1, 5)]
    monday = by_date[date(2024, 1, 8)]
    tuesday = by_date[date(2024, 1, 9)]
    thursday = by_date[date(2024, 1, 11)]
    assert friday.volatility_state is VolatilityState.HIGH
    assert friday.target_exposure == Decimal("1.60")
    assert friday.exposure_reviewed is False
    assert monday.target_exposure == Decimal("0.60")
    assert monday.exposure_reviewed is True
    assert monday.target_changed is True
    assert tuesday.volatility_state is VolatilityState.NORMAL
    assert tuesday.target_volatility_state is VolatilityState.HIGH
    assert tuesday.target_exposure == Decimal("0.60")
    assert thursday.target_exposure == Decimal("1.60")
    assert thursday.exposure_reviewed is True
    assert thursday.target_changed is True


def test_rv_exactly_at_threshold_selects_high_vol_exposure() -> None:
    close_values = [Decimal("100"), Decimal("110"), Decimal("99")]
    threshold = realized_volatility(
        close_values, window_sessions=2, annualization_sessions=1
    )
    selected = config(annualization_sessions=1, rv_threshold=threshold)

    initial = replay_strategy(bars("100", "110", "99"), selected).latest

    assert initial.metrics.rv == threshold
    assert initial.volatility_state is VolatilityState.HIGH
    assert initial.target_exposure == Decimal("0.60")


def test_incomplete_latest_bar_is_ignored() -> None:
    replay = replay_strategy(
        bars("100", "100", "100", "999", completed_last=False), config()
    )

    assert replay.latest.session_date == date(2024, 1, 4)
    assert replay.latest.metrics.spy_close == Decimal("100")


def test_replay_fails_closed_for_short_or_duplicate_history() -> None:
    with pytest.raises(CalculationUnavailable, match="at least 3"):
        replay_strategy(bars("100", "100"), config())

    duplicate = bars("100", "100", "100")
    duplicate[-1] = DailyBar(duplicate[-2].session_date, Decimal("100"))
    with pytest.raises(CalculationUnavailable, match="strictly increasing"):
        replay_strategy(duplicate, config())
