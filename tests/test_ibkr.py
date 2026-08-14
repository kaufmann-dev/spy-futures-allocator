from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from ibapi.common import TickAttrib
from ibapi.contract import Contract

from app.integrations.ibkr import (
    _QUOTE_REQUEST_IDS,
    AdjustedSpyDailyBar,
    FuturesQuote,
    IbkrMarketData,
    IbkrUnavailable,
    ResolvedContract,
    active_futures_expiry,
)


def futures_contract(
    symbol: str,
    *,
    expiry: str = "20260918",
    multiplier: Decimal | None = None,
) -> ResolvedContract:
    expected_multiplier = {"ES": Decimal("50"), "MES": Decimal("5")}[symbol]
    return ResolvedContract(
        con_id={"ES": 123, "MES": 456}[symbol],
        symbol=symbol,
        local_symbol=f"{symbol}U6",
        security_type="FUT",
        exchange="CME",
        currency="USD",
        trading_class=symbol,
        expiry=expiry,
        multiplier=expected_multiplier if multiplier is None else multiplier,
    )


def spy_contract() -> ResolvedContract:
    return ResolvedContract(
        con_id=789,
        symbol="SPY",
        local_symbol="SPY",
        security_type="STK",
        exchange="SMART",
        currency="USD",
        trading_class="SPY",
        expiry="",
        multiplier=Decimal(1),
    )


def connected_market_data(monkeypatch: pytest.MonkeyPatch) -> IbkrMarketData:
    market_data = IbkrMarketData("127.0.0.1", 4002, 10)
    market_data._client.ready.set()
    monkeypatch.setattr(market_data._client, "isConnected", lambda: True)
    return market_data


def test_active_contract_rolls_eight_calendar_days_before_third_friday() -> None:
    assert active_futures_expiry(date(2026, 9, 9)) == "202609"
    assert active_futures_expiry(date(2026, 9, 10)) == "202612"


def test_futures_mark_prefers_midpoint() -> None:
    quote = FuturesQuote(
        contract=futures_contract("ES"),
        bid=Decimal("6500.00"),
        ask=Decimal("6500.50"),
        last=Decimal("6499"),
        timestamp=datetime.now(UTC),
        market_data_type=1,
    )

    assert quote.mark == Decimal("6500.25")
    assert quote.is_live


@pytest.mark.parametrize(
    ("bid", "ask"),
    [
        (None, Decimal("6500.50")),
        (Decimal("6501.00"), Decimal("6500.50")),
    ],
)
def test_futures_mark_uses_last_without_valid_spread(
    bid: Decimal | None, ask: Decimal | None
) -> None:
    quote = FuturesQuote(
        contract=futures_contract("MES"),
        bid=bid,
        ask=ask,
        last=Decimal("6500.25"),
        timestamp=datetime.now(UTC),
        market_data_type=3,
    )

    assert quote.mark == Decimal("6500.25")
    assert not quote.is_live


def test_resolve_active_futures_requires_expected_multipliers_and_same_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market_data = IbkrMarketData("127.0.0.1", 4002, 10)
    requested: list[tuple[str, str]] = []

    def resolve(contract: Contract, timeout: float = 15.0) -> ResolvedContract:
        del timeout
        requested.append((str(contract.symbol), str(contract.lastTradeDateOrContractMonth)))
        return futures_contract(str(contract.symbol))

    monkeypatch.setattr(market_data, "resolve_contract", resolve)

    contracts = market_data.resolve_active_futures(date(2026, 8, 14))

    assert requested == [("ES", "202609"), ("MES", "202609")]
    assert contracts["ES"].multiplier == Decimal("50")
    assert contracts["MES"].multiplier == Decimal("5")

    def wrong_multiplier(contract: Contract, timeout: float = 15.0) -> ResolvedContract:
        del timeout
        symbol = str(contract.symbol)
        if symbol == "ES":
            return futures_contract(symbol, multiplier=Decimal("5"))
        return futures_contract(symbol)

    monkeypatch.setattr(market_data, "resolve_contract", wrong_multiplier)
    with pytest.raises(IbkrUnavailable, match="ES identity or multiplier"):
        market_data.resolve_active_futures(date(2026, 8, 14))

    def mismatched_expiry(contract: Contract, timeout: float = 15.0) -> ResolvedContract:
        del timeout
        symbol = str(contract.symbol)
        expiry = "20260917" if symbol == "MES" else "20260918"
        return futures_contract(symbol, expiry=expiry)

    monkeypatch.setattr(market_data, "resolve_contract", mismatched_expiry)
    with pytest.raises(IbkrUnavailable, match="same expiry"):
        market_data.resolve_active_futures(date(2026, 8, 14))


def test_dual_subscriptions_use_distinct_request_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market_data = connected_market_data(monkeypatch)
    subscriptions: list[tuple[int, str]] = []
    monkeypatch.setattr(market_data._client, "reqMarketDataType", lambda data_type: None)

    def subscribe(
        request_id: int,
        contract: Contract,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[Any],
    ) -> None:
        del generic_ticks, snapshot, regulatory_snapshot, options
        subscriptions.append((request_id, str(contract.symbol)))

    monkeypatch.setattr(market_data._client, "reqMktData", subscribe)

    market_data.subscribe_futures(
        {symbol: futures_contract(symbol) for symbol in ("ES", "MES")}
    )

    assert subscriptions == [
        (_QUOTE_REQUEST_IDS["ES"], "ES"),
        (_QUOTE_REQUEST_IDS["MES"], "MES"),
    ]
    assert _QUOTE_REQUEST_IDS["ES"] != _QUOTE_REQUEST_IDS["MES"]


def test_quote_callbacks_keep_es_and_mes_state_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market_data = connected_market_data(monkeypatch)
    market_data._futures_contracts = {
        symbol: futures_contract(symbol) for symbol in ("ES", "MES")
    }
    client = market_data._client
    tick_attributes = TickAttrib()

    client.marketDataType(_QUOTE_REQUEST_IDS["ES"], 1)
    client.tickPrice(_QUOTE_REQUEST_IDS["ES"], 1, 6500.0, tick_attributes)
    client.tickPrice(_QUOTE_REQUEST_IDS["ES"], 2, 6500.5, tick_attributes)
    client.tickString(_QUOTE_REQUEST_IDS["ES"], 45, "1786723200")
    client.marketDataType(_QUOTE_REQUEST_IDS["MES"], 3)
    client.tickPrice(_QUOTE_REQUEST_IDS["MES"], 4, 6499.25, tick_attributes)
    client.tickString(_QUOTE_REQUEST_IDS["MES"], 45, "1786723260")

    quotes = market_data.current_futures_quotes()

    assert quotes["ES"].bid == Decimal("6500.0")
    assert quotes["ES"].ask == Decimal("6500.5")
    assert quotes["ES"].last is None
    assert quotes["ES"].mark == Decimal("6500.25")
    assert quotes["ES"].is_live
    assert quotes["MES"].bid is None
    assert quotes["MES"].ask is None
    assert quotes["MES"].last == Decimal("6499.25")
    assert quotes["MES"].mark == Decimal("6499.25")
    assert not quotes["MES"].is_live
    assert quotes["ES"].timestamp != quotes["MES"].timestamp


def test_disconnect_cancels_both_futures_subscriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market_data = connected_market_data(monkeypatch)
    cancelled: list[int] = []
    monkeypatch.setattr(market_data._client, "cancelMktData", cancelled.append)
    monkeypatch.setattr(market_data._client, "disconnect", lambda: None)

    market_data.disconnect()

    assert cancelled == [_QUOTE_REQUEST_IDS["ES"], _QUOTE_REQUEST_IDS["MES"]]


def test_adjusted_spy_history_uses_exact_request_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market_data = connected_market_data(monkeypatch)
    client = market_data._client
    monkeypatch.setattr(market_data, "resolve_spy", spy_contract)
    requested: list[tuple[str, str, str, str, int, int, bool, list[Any]]] = []

    def request_history(
        request_id: int,
        contract: Contract,
        end: str,
        duration: str,
        bar_size: str,
        what_to_show: str,
        use_rth: int,
        format_date: int,
        keep_up_to_date: bool,
        options: list[Any],
    ) -> None:
        assert contract.symbol == "SPY"
        requested.append(
            (end, duration, bar_size, what_to_show, use_rth, format_date, keep_up_to_date, options)
        )
        client.historicalData(
            request_id,
            SimpleNamespace(
                date="20260813",
                open=640,
                high=645,
                low=639,
                close=644,
                volume=1_000,
            ),
        )
        client.historicalData(
            request_id,
            SimpleNamespace(
                date="20260813",
                open=640,
                high=646,
                low=639,
                close=645,
                volume=1_100,
            ),
        )
        client.historicalData(
            request_id,
            SimpleNamespace(
                date="20260814",
                open=646,
                high=648,
                low=644,
                close=647,
                volume=1_200,
            ),
        )
        client.historicalDataEnd(request_id, "", "")

    monkeypatch.setattr(client, "reqHistoricalData", request_history)

    bars = market_data.fetch_adjusted_spy_daily_bars()

    assert requested == [("", "40 Y", "1 day", "ADJUSTED_LAST", 1, 2, False, [])]
    assert bars == [
        AdjustedSpyDailyBar(
            session_date=date(2026, 8, 13),
            open=Decimal("640"),
            high=Decimal("646"),
            low=Decimal("639"),
            close=Decimal("645"),
            volume=Decimal("1100"),
        ),
        AdjustedSpyDailyBar(
            session_date=date(2026, 8, 14),
            open=Decimal("646"),
            high=Decimal("648"),
            low=Decimal("644"),
            close=Decimal("647"),
            volume=Decimal("1200"),
        ),
    ]
