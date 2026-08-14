"""Pure SPY regime and realized-volatility strategy replay."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from enum import StrEnum

from .errors import CalculationUnavailable, DomainError
from .types import DailyBar, StrategyConfig

ZERO = Decimal("0")
ONE = Decimal("1")


class MarketRegime(StrEnum):
    """Whether the strategy permits long S&P 500 exposure."""

    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"


class VolatilityState(StrEnum):
    """Current realized-volatility classification."""

    NORMAL = "normal"
    HIGH = "high"


class RegimeTransition(StrEnum):
    """A state transition observed at a completed SPY close."""

    INITIALIZED = "initialized"
    NONE = "none"
    BEARISH_EXIT = "bearish_exit"
    REBOUND_REENTRY = "rebound_reentry"


@dataclass(frozen=True, slots=True)
class StrategyMetrics:
    """Signal metrics calculated at one completed SPY close."""

    spy_close: Decimal
    fast_sma: Decimal
    slow_sma: Decimal
    rv: Decimal


@dataclass(frozen=True, slots=True)
class StrategySnapshot:
    """Complete strategy state after processing one completed session close.

    ``target_changed`` marks a target instruction that becomes effective at the
    next actual XNYS session open. ``volatility_state`` describes today's RV;
    ``target_volatility_state`` describes the last RV bucket used to set the
    held target and can therefore differ between configured review sessions.
    """

    session_date: date
    metrics: StrategyMetrics
    regime: MarketRegime
    volatility_state: VolatilityState
    target_volatility_state: VolatilityState | None
    target_exposure: Decimal
    exit_armed: bool
    post_exit_trough: Decimal | None
    reentry_level: Decimal | None
    transition: RegimeTransition
    exposure_reviewed: bool
    target_changed: bool
    exit_rearmed: bool


@dataclass(frozen=True, slots=True)
class StrategyReplay:
    """A deterministic replay under one canonical strategy configuration."""

    config: StrategyConfig
    snapshots: tuple[StrategySnapshot, ...]

    @property
    def latest(self) -> StrategySnapshot:
        if not self.snapshots:  # pragma: no cover - constructor is internal
            raise CalculationUnavailable("strategy replay is empty")
        return self.snapshots[-1]

    @property
    def instructions(self) -> tuple[StrategySnapshot, ...]:
        """Snapshots whose target must be scheduled for the next session open."""

        return tuple(snapshot for snapshot in self.snapshots if snapshot.target_changed)


def _completed_bars(bars: Iterable[DailyBar], config: StrategyConfig) -> list[DailyBar]:
    materialized = list(bars)
    if not materialized:
        raise CalculationUnavailable("adjusted SPY history is missing")

    previous_date = None
    completed: list[DailyBar] = []
    for bar in materialized:
        if not isinstance(bar, DailyBar):
            raise CalculationUnavailable("adjusted SPY history contains an invalid bar")
        if previous_date is not None and bar.session_date <= previous_date:
            raise CalculationUnavailable(
                "SPY bars must have unique, strictly increasing session dates"
            )
        previous_date = bar.session_date
        if bar.completed:
            completed.append(bar)

    if len(completed) < config.minimum_history_sessions:
        raise CalculationUnavailable(
            f"at least {config.minimum_history_sessions} completed adjusted SPY bars are required"
        )
    return completed


def _sample_standard_deviation(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        raise CalculationUnavailable("at least two return observations are required")
    mean = sum(values, ZERO) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), ZERO) / Decimal(len(values) - 1)
    return variance.sqrt()


def realized_volatility(
    closes: Sequence[Decimal],
    *,
    window_sessions: int,
    annualization_sessions: int,
) -> Decimal:
    """Calculate annualized sample-standard-deviation RV from simple returns."""

    if len(closes) < window_sessions + 1:
        raise CalculationUnavailable(
            f"at least {window_sessions + 1} closes are required for RV{window_sessions}"
        )
    with localcontext() as context:
        context.prec = 50
        returns = [
            closes[index] / closes[index - 1] - ONE
            for index in range(len(closes) - window_sessions, len(closes))
        ]
        return _sample_standard_deviation(returns) * Decimal(annualization_sessions).sqrt()


def _volatility_state(rv: Decimal, config: StrategyConfig) -> VolatilityState:
    return VolatilityState.HIGH if rv >= config.rv_threshold else VolatilityState.NORMAL


def _exposure_for(state: VolatilityState, config: StrategyConfig) -> Decimal:
    if state is VolatilityState.HIGH:
        return config.high_vol_exposure
    return config.normal_exposure


def replay_strategy(
    bars: Iterable[DailyBar], config: StrategyConfig | None = None
) -> StrategyReplay:
    """Replay the regime state machine over adjusted, completed SPY closes."""

    selected_config = config or StrategyConfig()
    if not isinstance(selected_config, StrategyConfig):
        raise CalculationUnavailable("config must be a StrategyConfig")

    try:
        ordered = _completed_bars(bars, selected_config)
        closes = [bar.close for bar in ordered]
        first_index = selected_config.minimum_history_sessions - 1
        snapshots: list[StrategySnapshot] = []

        regime: MarketRegime | None = None
        target_state: VolatilityState | None = None
        target_exposure = ZERO
        exit_armed = False
        trough: Decimal | None = None
        previous_fast: Decimal | None = None
        previous_slow: Decimal | None = None

        with localcontext() as context:
            context.prec = 50
            for index in range(first_index, len(ordered)):
                bar = ordered[index]
                fast = sum(
                    closes[index - selected_config.fast_sma_sessions + 1 : index + 1], ZERO
                ) / Decimal(selected_config.fast_sma_sessions)
                slow = sum(
                    closes[index - selected_config.slow_sma_sessions + 1 : index + 1], ZERO
                ) / Decimal(selected_config.slow_sma_sessions)
                rv = realized_volatility(
                    closes[: index + 1],
                    window_sessions=selected_config.rv_window_sessions,
                    annualization_sessions=selected_config.annualization_sessions,
                )
                current_volatility = _volatility_state(rv, selected_config)
                transition = RegimeTransition.NONE
                reviewed = False
                target_changed = False
                exit_rearmed = False
                snapshot_trough: Decimal | None = None
                snapshot_reentry: Decimal | None = None

                if regime is None:
                    transition = RegimeTransition.INITIALIZED
                    target_changed = True
                    if fast >= slow:
                        regime = MarketRegime.RISK_ON
                        exit_armed = True
                        target_state = current_volatility
                        target_exposure = _exposure_for(current_volatility, selected_config)
                    else:
                        regime = MarketRegime.RISK_OFF
                        exit_armed = False
                        target_state = None
                        target_exposure = ZERO
                        trough = bar.close
                        snapshot_trough = trough
                        snapshot_reentry = trough * (ONE + selected_config.rebound_fraction)
                elif regime is MarketRegime.RISK_OFF:
                    if trough is None:  # pragma: no cover - internal invariant
                        raise CalculationUnavailable("risk-off state is missing its trough")
                    trough = min(trough, bar.close)
                    reentry_level = trough * (ONE + selected_config.rebound_fraction)
                    snapshot_trough = trough
                    snapshot_reentry = reentry_level
                    if bar.close >= reentry_level:
                        regime = MarketRegime.RISK_ON
                        transition = RegimeTransition.REBOUND_REENTRY
                        target_state = current_volatility
                        target_exposure = _exposure_for(current_volatility, selected_config)
                        target_changed = True
                        exit_armed = fast > slow
                        exit_rearmed = exit_armed
                        trough = None
                else:
                    if not exit_armed and fast > slow:
                        exit_armed = True
                        exit_rearmed = True

                    bearish_cross = (
                        exit_armed
                        and previous_fast is not None
                        and previous_slow is not None
                        and previous_fast >= previous_slow
                        and fast < slow
                    )
                    if bearish_cross:
                        regime = MarketRegime.RISK_OFF
                        transition = RegimeTransition.BEARISH_EXIT
                        target_state = None
                        target_exposure = ZERO
                        target_changed = True
                        exit_armed = False
                        trough = bar.close
                        snapshot_trough = trough
                        snapshot_reentry = trough * (ONE + selected_config.rebound_fraction)
                    elif selected_config.reviews_on(bar.session_date):
                        reviewed = True
                        reviewed_target = _exposure_for(current_volatility, selected_config)
                        target_changed = reviewed_target != target_exposure
                        target_state = current_volatility
                        target_exposure = reviewed_target

                snapshots.append(
                    StrategySnapshot(
                        session_date=bar.session_date,
                        metrics=StrategyMetrics(
                            spy_close=bar.close,
                            fast_sma=fast,
                            slow_sma=slow,
                            rv=rv,
                        ),
                        regime=regime,
                        volatility_state=current_volatility,
                        target_volatility_state=target_state,
                        target_exposure=target_exposure,
                        exit_armed=exit_armed,
                        post_exit_trough=snapshot_trough,
                        reentry_level=snapshot_reentry,
                        transition=transition,
                        exposure_reviewed=reviewed,
                        target_changed=target_changed,
                        exit_rearmed=exit_rearmed,
                    )
                )
                previous_fast = fast
                previous_slow = slow

        return StrategyReplay(config=selected_config, snapshots=tuple(snapshots))
    except CalculationUnavailable:
        raise
    except DomainError as exc:
        raise CalculationUnavailable(str(exc)) from exc
    except (ArithmeticError, ValueError) as exc:
        raise CalculationUnavailable("SPY strategy state could not be calculated") from exc


def calculate_strategy(
    bars: Iterable[DailyBar], config: StrategyConfig | None = None
) -> StrategySnapshot:
    """Return the latest snapshot from a full deterministic replay."""

    return replay_strategy(bars, config).latest
