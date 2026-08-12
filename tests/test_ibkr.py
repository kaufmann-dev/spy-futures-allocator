from datetime import UTC, date, datetime
from decimal import Decimal

from app.integrations.ibkr import MesQuote, ResolvedContract, active_mes_expiry


def mes_contract() -> ResolvedContract:
    return ResolvedContract(
        con_id=123,
        symbol="MES",
        local_symbol="MESU6",
        security_type="FUT",
        exchange="CME",
        currency="USD",
        trading_class="MES",
        expiry="20260918",
        multiplier=Decimal("5"),
    )


def test_active_contract_rolls_eight_calendar_days_before_third_friday() -> None:
    assert active_mes_expiry(date(2026, 9, 9)) == "202609"
    assert active_mes_expiry(date(2026, 9, 10)) == "202612"


def test_mes_mark_prefers_midpoint() -> None:
    quote = MesQuote(
        contract=mes_contract(),
        bid=Decimal("6500.00"),
        ask=Decimal("6500.50"),
        last=Decimal("6499"),
        timestamp=datetime.now(UTC),
        market_data_type=1,
    )
    assert quote.mark == Decimal("6500.25")
    assert quote.is_live


def test_mes_mark_uses_last_only_without_valid_spread() -> None:
    quote = MesQuote(
        contract=mes_contract(),
        bid=None,
        ask=Decimal("6500.50"),
        last=Decimal("6500.25"),
        timestamp=datetime.now(UTC),
        market_data_type=3,
    )
    assert quote.mark == Decimal("6500.25")
    assert not quote.is_live
