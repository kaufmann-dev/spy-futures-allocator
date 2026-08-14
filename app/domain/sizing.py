"""Non-overshooting ES-first, MES-remainder futures sizing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, localcontext

from .errors import DomainError, SizingUnavailable
from .types import DecimalLike, decimal_value

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class FuturesSizingResult:
    """Target and achieved notional plus signed position adjustments."""

    recommended_es: int
    recommended_mes: int
    es_mark: Decimal
    mes_mark: Decimal
    es_multiplier: Decimal
    mes_multiplier: Decimal
    es_contract_notional: Decimal
    mes_contract_notional: Decimal
    target_exposure: Decimal
    target_notional: Decimal
    achieved_notional: Decimal
    achieved_exposure: Decimal
    notional_shortfall: Decimal
    exposure_shortfall: Decimal
    current_es_quantity: int | None
    current_mes_quantity: int | None
    es_quantity_difference: int | None
    mes_quantity_difference: int | None


def _current_quantity(value: int | None, *, field: str) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise SizingUnavailable(f"{field} must be an integer")
    return value


def size_futures(
    *,
    strategy_amount_usd: DecimalLike,
    target_exposure: DecimalLike,
    es_mark: DecimalLike,
    es_multiplier: DecimalLike,
    mes_mark: DecimalLike,
    mes_multiplier: DecimalLike,
    current_es_quantity: int | None = None,
    current_mes_quantity: int | None = None,
) -> FuturesSizingResult:
    """Allocate ES first and floor MES against the remaining target notional."""

    try:
        amount = decimal_value(strategy_amount_usd, field="strategy_amount_usd")
        target = decimal_value(target_exposure, field="target_exposure")
        es_price = decimal_value(es_mark, field="es_mark")
        es_factor = decimal_value(es_multiplier, field="es_multiplier")
        mes_price = decimal_value(mes_mark, field="mes_mark")
        mes_factor = decimal_value(mes_multiplier, field="mes_multiplier")
    except DomainError as exc:
        raise SizingUnavailable(str(exc)) from exc

    if amount <= ZERO:
        raise SizingUnavailable("strategy_amount_usd must be positive")
    if target < ZERO:
        raise SizingUnavailable("target_exposure must be nonnegative")
    if es_price <= ZERO or es_factor <= ZERO:
        raise SizingUnavailable("a positive ES mark and multiplier are required")
    if mes_price <= ZERO or mes_factor <= ZERO:
        raise SizingUnavailable("a positive MES mark and multiplier are required")
    current_es = _current_quantity(current_es_quantity, field="current_es_quantity")
    current_mes = _current_quantity(current_mes_quantity, field="current_mes_quantity")

    with localcontext() as context:
        context.prec = 50
        es_notional = es_price * es_factor
        mes_notional = mes_price * mes_factor
        target_notional = amount * target

        recommended_es = int(
            (target_notional / es_notional).to_integral_value(rounding=ROUND_FLOOR)
        )
        es_total = Decimal(recommended_es) * es_notional
        remaining_notional = target_notional - es_total
        recommended_mes = int(
            (remaining_notional / mes_notional).to_integral_value(rounding=ROUND_FLOOR)
        )
        recommended_es = max(0, recommended_es)
        recommended_mes = max(0, recommended_mes)

        achieved_notional = (
            Decimal(recommended_es) * es_notional + Decimal(recommended_mes) * mes_notional
        )
        achieved_exposure = achieved_notional / amount
        if achieved_notional > target_notional or achieved_exposure > target:
            raise SizingUnavailable("integer futures sizing would exceed the target")
        notional_shortfall = target_notional - achieved_notional
        exposure_shortfall = target - achieved_exposure

    return FuturesSizingResult(
        recommended_es=recommended_es,
        recommended_mes=recommended_mes,
        es_mark=es_price,
        mes_mark=mes_price,
        es_multiplier=es_factor,
        mes_multiplier=mes_factor,
        es_contract_notional=es_notional,
        mes_contract_notional=mes_notional,
        target_exposure=target,
        target_notional=target_notional,
        achieved_notional=achieved_notional,
        achieved_exposure=achieved_exposure,
        notional_shortfall=notional_shortfall,
        exposure_shortfall=exposure_shortfall,
        current_es_quantity=current_es,
        current_mes_quantity=current_mes,
        es_quantity_difference=(
            recommended_es - current_es if current_es is not None else None
        ),
        mes_quantity_difference=(
            recommended_mes - current_mes if current_mes is not None else None
        ),
    )
