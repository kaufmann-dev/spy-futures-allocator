from decimal import Decimal, localcontext
from typing import Any

import pytest

from app.domain.errors import SizingUnavailable
from app.domain.sizing import size_futures


def test_es_is_allocated_first_and_mes_fills_the_remainder() -> None:
    result = size_futures(
        strategy_amount_usd="1000000",
        target_exposure="1.60",
        es_mark="5000",
        es_multiplier="50",
        mes_mark="5000",
        mes_multiplier="5",
    )

    assert result.recommended_es == 6
    assert result.recommended_mes == 4
    assert result.es_contract_notional == Decimal("250000")
    assert result.mes_contract_notional == Decimal("25000")
    assert result.target_notional == Decimal("1600000.00")
    assert result.achieved_notional == Decimal("1600000")
    assert result.achieved_exposure == Decimal("1.6")
    assert result.notional_shortfall == 0
    assert result.exposure_shortfall == 0


def test_nonexact_sizing_floors_both_contracts_and_never_overshoots() -> None:
    result = size_futures(
        strategy_amount_usd="999999",
        target_exposure="1.60",
        es_mark="5000",
        es_multiplier="50",
        mes_mark="5000",
        mes_multiplier="5",
    )

    assert result.recommended_es == 6
    assert result.recommended_mes == 3
    assert result.achieved_notional == Decimal("1575000")
    assert result.achieved_notional < result.target_notional
    assert result.achieved_exposure < result.target_exposure
    assert result.notional_shortfall == result.target_notional - result.achieved_notional
    with localcontext() as context:
        context.prec = 50
        assert result.exposure_shortfall == result.target_exposure - result.achieved_exposure


def test_each_contract_uses_its_actual_mark_and_multiplier() -> None:
    result = size_futures(
        strategy_amount_usd="1000000",
        target_exposure="1.6",
        es_mark="5100",
        es_multiplier="50",
        mes_mark="5098",
        mes_multiplier="5",
    )

    assert result.es_contract_notional == Decimal("255000")
    assert result.mes_contract_notional == Decimal("25490")
    assert result.recommended_es == 6
    assert result.recommended_mes == 2
    assert result.achieved_notional == Decimal("1580980")
    assert result.achieved_notional <= result.target_notional


def test_zero_target_produces_zero_contracts() -> None:
    result = size_futures(
        strategy_amount_usd="100000",
        target_exposure="0",
        es_mark="5000",
        es_multiplier="50",
        mes_mark="5000",
        mes_multiplier="5",
    )

    assert result.recommended_es == 0
    assert result.recommended_mes == 0
    assert result.target_notional == 0
    assert result.achieved_exposure == 0


def test_signed_current_quantities_produce_independent_signed_differences() -> None:
    result = size_futures(
        strategy_amount_usd="1000000",
        target_exposure="1.6",
        es_mark="5000",
        es_multiplier="50",
        mes_mark="5000",
        mes_multiplier="5",
        current_es_quantity=7,
        current_mes_quantity=-2,
    )

    assert result.current_es_quantity == 7
    assert result.current_mes_quantity == -2
    assert result.es_quantity_difference == -1
    assert result.mes_quantity_difference == 6


def test_no_legacy_maximum_rejects_an_arbitrary_positive_target() -> None:
    result = size_futures(
        strategy_amount_usd="100000",
        target_exposure="9",
        es_mark="5000",
        es_multiplier="50",
        mes_mark="5000",
        mes_multiplier="5",
    )

    assert result.target_exposure == Decimal("9")
    assert result.achieved_exposure <= Decimal("9")


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "strategy_amount_usd": "0",
            "target_exposure": "1",
            "es_mark": "5000",
            "es_multiplier": "50",
            "mes_mark": "5000",
            "mes_multiplier": "5",
        },
        {
            "strategy_amount_usd": "100000",
            "target_exposure": "-0.01",
            "es_mark": "5000",
            "es_multiplier": "50",
            "mes_mark": "5000",
            "mes_multiplier": "5",
        },
        {
            "strategy_amount_usd": "100000",
            "target_exposure": "1",
            "es_mark": "0",
            "es_multiplier": "50",
            "mes_mark": "5000",
            "mes_multiplier": "5",
        },
        {
            "strategy_amount_usd": "100000",
            "target_exposure": "1",
            "es_mark": "5000",
            "es_multiplier": "50",
            "mes_mark": "5000",
            "mes_multiplier": "nan",
        },
    ],
)
def test_sizing_fails_closed_for_invalid_numeric_inputs(kwargs: dict[str, Any]) -> None:
    with pytest.raises(SizingUnavailable):
        size_futures(**kwargs)


def test_current_quantities_must_be_integers() -> None:
    with pytest.raises(SizingUnavailable, match="current_es_quantity"):
        size_futures(
            strategy_amount_usd="100000",
            target_exposure="1",
            es_mark="5000",
            es_multiplier="50",
            mes_mark="5000",
            mes_multiplier="5",
            current_es_quantity=True,
        )
