from decimal import Decimal, localcontext
from typing import Any

import pytest

from app.domain.errors import SizingUnavailable
from app.domain.sizing import size_mes


def test_integer_sizing_floors_and_never_exceeds_target() -> None:
    result = size_mes(
        strategy_amount_usd=Decimal("99999"),
        final_target=Decimal("1"),
        mes_mark=Decimal("5000"),
        mes_multiplier=Decimal("5"),
    )

    assert result.recommended_mes == 3
    assert result.mes_contract_notional == Decimal("25000")
    assert result.achieved_notional == Decimal("75000")
    assert result.achieved_leverage < Decimal("1")
    with localcontext() as context:
        context.prec = 40
        assert result.target_shortfall == Decimal("1") - result.achieved_leverage


def test_exact_contract_boundary_is_allowed() -> None:
    result = size_mes(
        strategy_amount_usd="100000",
        final_target="1",
        mes_mark="5000",
        mes_multiplier="5",
    )
    assert result.recommended_mes == 4
    assert result.achieved_leverage == Decimal("1")


def test_zero_target_produces_zero_mes() -> None:
    result = size_mes(
        strategy_amount_usd="100000",
        final_target="0",
        mes_mark="5000",
        mes_multiplier="5",
    )
    assert result.recommended_mes == 0
    assert result.target_notional == 0
    assert result.achieved_leverage == 0


def test_current_quantity_is_display_only_difference() -> None:
    result = size_mes(
        strategy_amount_usd="100000",
        final_target="1",
        mes_mark="5000",
        mes_multiplier="5",
        current_mes_quantity=6,
    )
    assert result.current_mes_quantity == 6
    assert result.quantity_difference == -2


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "strategy_amount_usd": "0",
            "final_target": "1",
            "mes_mark": "5000",
            "mes_multiplier": "5",
        },
        {
            "strategy_amount_usd": "100000",
            "final_target": "4.85001",
            "mes_mark": "5000",
            "mes_multiplier": "5",
        },
        {
            "strategy_amount_usd": "100000",
            "final_target": "1",
            "mes_mark": "0",
            "mes_multiplier": "5",
        },
        {
            "strategy_amount_usd": "100000",
            "final_target": "1",
            "mes_mark": "5000",
            "mes_multiplier": "nan",
        },
    ],
)
def test_sizing_fails_closed_for_invalid_inputs(kwargs: dict[str, Any]) -> None:
    with pytest.raises(SizingUnavailable):
        size_mes(**kwargs)
