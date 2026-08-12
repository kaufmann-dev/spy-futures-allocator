"""Non-overshooting MES notional sizing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, localcontext

from .calculation import MAX_TARGET, ZERO
from .errors import DomainError, SizingUnavailable
from .types import DecimalLike, decimal_value


@dataclass(frozen=True, slots=True)
class MesSizingResult:
    recommended_mes: int
    mes_contract_notional: Decimal
    target_notional: Decimal
    achieved_notional: Decimal
    achieved_leverage: Decimal
    target_shortfall: Decimal
    current_mes_quantity: int | None = None
    quantity_difference: int | None = None


def size_mes(
    *,
    strategy_amount_usd: DecimalLike,
    final_target: DecimalLike,
    mes_mark: DecimalLike,
    mes_multiplier: DecimalLike,
    current_mes_quantity: int | None = None,
) -> MesSizingResult:
    """Floor an MES quantity so achieved leverage can never exceed its target."""

    try:
        amount = decimal_value(strategy_amount_usd, field="strategy_amount_usd")
        target = decimal_value(final_target, field="final_target")
        mark = decimal_value(mes_mark, field="mes_mark")
        multiplier = decimal_value(mes_multiplier, field="mes_multiplier")
    except DomainError as exc:
        raise SizingUnavailable(str(exc)) from exc

    if amount <= ZERO:
        raise SizingUnavailable("strategy_amount_usd must be positive")
    if target < ZERO or target > MAX_TARGET:
        raise SizingUnavailable("final_target must be between 0 and 4.85")
    if mark <= ZERO or multiplier <= ZERO:
        raise SizingUnavailable("a positive MES mark and multiplier are required")
    if current_mes_quantity is not None and (
        isinstance(current_mes_quantity, bool) or not isinstance(current_mes_quantity, int)
    ):
        raise SizingUnavailable("current_mes_quantity must be an integer")

    with localcontext() as context:
        context.prec = 40
        contract_notional = mark * multiplier
        target_notional = amount * target
        recommended = int(
            (target_notional / contract_notional).to_integral_value(rounding=ROUND_FLOOR)
        )
        recommended = max(0, recommended)
        achieved_notional = Decimal(recommended) * contract_notional
        achieved_leverage = achieved_notional / amount
        if achieved_leverage > target:
            raise SizingUnavailable("integer sizing would exceed the target")
        shortfall = target - achieved_leverage

    difference = recommended - current_mes_quantity if current_mes_quantity is not None else None
    return MesSizingResult(
        recommended_mes=recommended,
        mes_contract_notional=contract_notional,
        target_notional=target_notional,
        achieved_notional=achieved_notional,
        achieved_leverage=achieved_leverage,
        target_shortfall=shortfall,
        current_mes_quantity=current_mes_quantity,
        quantity_difference=difference,
    )
