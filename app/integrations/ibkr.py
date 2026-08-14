from __future__ import annotations

import calendar
import math
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from ibapi.client import EClient
from ibapi.common import TickAttrib
from ibapi.contract import Contract, ContractDetails
from ibapi.wrapper import EWrapper


class IbkrUnavailable(RuntimeError):
    pass


NEW_YORK = ZoneInfo("America/New_York")
FUTURES_SYMBOLS = ("ES", "MES")
_EXPECTED_MULTIPLIERS = {
    "ES": Decimal("50"),
    "MES": Decimal("5"),
}
_QUOTE_REQUEST_IDS = {
    "ES": 40_001,
    "MES": 40_002,
}


@dataclass(frozen=True)
class ResolvedContract:
    con_id: int
    symbol: str
    local_symbol: str
    security_type: str
    exchange: str
    currency: str
    trading_class: str
    expiry: str
    multiplier: Decimal


@dataclass(frozen=True)
class AdjustedSpyDailyBar:
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class FuturesQuote:
    contract: ResolvedContract
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    timestamp: datetime
    market_data_type: int | None

    @property
    def mark(self) -> Decimal | None:
        if (
            self.bid is not None
            and self.ask is not None
            and self.bid > 0
            and self.ask > 0
            and self.bid <= self.ask
        ):
            return (self.bid + self.ask) / Decimal(2)
        if self.last is not None and self.last > 0:
            return self.last
        return None

    @property
    def is_live(self) -> bool:
        return self.market_data_type == 1


@dataclass
class _ContractRequest:
    event: threading.Event = field(default_factory=threading.Event)
    details: list[ContractDetails] = field(default_factory=list)
    error: str | None = None


@dataclass
class _HistoricalRequest:
    event: threading.Event = field(default_factory=threading.Event)
    bars: list[AdjustedSpyDailyBar] = field(default_factory=list)
    error: str | None = None


@dataclass
class _QuoteState:
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    timestamp: datetime | None = None
    market_data_type: int | None = None


def _third_friday(year: int, month: int) -> date:
    month_calendar = calendar.monthcalendar(year, month)
    fridays = [week[calendar.FRIDAY] for week in month_calendar if week[calendar.FRIDAY]]
    return date(year, month, fridays[2])


def active_futures_expiry(on_date: date) -> str:
    """Return YYYYMM for ES and MES, rolling eight calendar days before expiry."""

    quarter_months = (3, 6, 9, 12)
    candidates = [(on_date.year, month) for month in quarter_months]
    candidates.extend((on_date.year + 1, month) for month in quarter_months)
    for year, month in candidates:
        expiry = _third_friday(year, month)
        if on_date < expiry - timedelta(days=8):
            return f"{year:04d}{month:02d}"
    raise AssertionError("quarterly futures expiry calculation failed")


def _decimal(value: Any) -> Decimal | None:
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return converted if converted.is_finite() else None


def _expiry_month(contract: ResolvedContract) -> str:
    expiry_month = contract.expiry[:6]
    if len(expiry_month) != 6 or not expiry_month.isdigit():
        raise IbkrUnavailable(f"IBKR returned an invalid {contract.symbol} expiry")
    return expiry_month


def _validate_futures_contracts(
    contracts: Mapping[str, ResolvedContract],
) -> dict[str, ResolvedContract]:
    if set(contracts) != set(FUTURES_SYMBOLS):
        raise IbkrUnavailable("both ES and MES contracts are required")

    validated: dict[str, ResolvedContract] = {}
    for symbol in FUTURES_SYMBOLS:
        contract = contracts[symbol]
        if (
            contract.symbol != symbol
            or contract.security_type != "FUT"
            or contract.multiplier != _EXPECTED_MULTIPLIERS[symbol]
        ):
            raise IbkrUnavailable(
                f"resolved {symbol} identity or multiplier is not the expected official contract"
            )
        validated[symbol] = contract

    expiry_values = {contract.expiry for contract in validated.values()}
    expiry_months = {_expiry_month(contract) for contract in validated.values()}
    if len(expiry_values) != 1 or len(expiry_months) != 1:
        raise IbkrUnavailable("resolved ES and MES contracts do not have the same expiry")
    return validated


class _IbkrClient(EWrapper, EClient):  # type: ignore[misc]
    def __init__(self) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self._lock = threading.RLock()
        self._request_id = 1_000
        self._contract_requests: dict[int, _ContractRequest] = {}
        self._historical_requests: dict[int, _HistoricalRequest] = {}
        self._quote_states = {
            request_id: _QuoteState() for request_id in _QUOTE_REQUEST_IDS.values()
        }
        self.last_error: str | None = None

    def next_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def reset_quote(self, request_id: int) -> None:
        with self._lock:
            if request_id not in self._quote_states:
                raise ValueError(f"unknown futures quote request ID: {request_id}")
            self._quote_states[request_id] = _QuoteState()

    def quote_state(self, request_id: int) -> _QuoteState:
        with self._lock:
            try:
                state = self._quote_states[request_id]
            except KeyError as exc:
                raise ValueError(f"unknown futures quote request ID: {request_id}") from exc
            return _QuoteState(
                bid=state.bid,
                ask=state.ask,
                last=state.last,
                timestamp=state.timestamp,
                market_data_type=state.market_data_type,
            )

    def nextValidId(self, orderId: int) -> None:  # noqa: N802, ARG002
        self.ready.set()

    def connectionClosed(self) -> None:  # noqa: N802
        self.ready.clear()

    def error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:  # noqa: N802, ARG002
        message = f"IBKR {errorCode}: {errorString}"
        # Informational farm/connectivity messages are not request failures.
        if errorCode in {2103, 2104, 2105, 2106, 2107, 2108, 2158}:
            return
        with self._lock:
            self.last_error = message
            contract_request = self._contract_requests.get(reqId)
            if contract_request is not None:
                contract_request.error = message
                contract_request.event.set()
            historical_request = self._historical_requests.get(reqId)
            if historical_request is not None:
                historical_request.error = message
                historical_request.event.set()

    def contractDetails(self, reqId: int, contractDetails: ContractDetails) -> None:  # noqa: N802
        with self._lock:
            if request := self._contract_requests.get(reqId):
                request.details.append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        with self._lock:
            if request := self._contract_requests.get(reqId):
                request.event.set()

    def historicalData(self, reqId: int, bar: Any) -> None:  # noqa: N802
        try:
            session_date = date.fromisoformat(str(bar.date)[:10].replace(" ", "-"))
            values = [
                _decimal(getattr(bar, name)) for name in ("open", "high", "low", "close", "volume")
            ]
            if any(value is None for value in values):
                return
            open_, high, low, close, volume = values
            assert open_ is not None and high is not None and low is not None
            assert close is not None and volume is not None
            if min(open_, high, low, close) <= 0 or volume < 0 or high < max(open_, low, close):
                return
            if low > min(open_, high, close):
                return
            parsed = AdjustedSpyDailyBar(session_date, open_, high, low, close, volume)
        except (AttributeError, ValueError, TypeError):
            return
        with self._lock:
            if request := self._historical_requests.get(reqId):
                request.bars.append(parsed)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:  # noqa: N802, ARG002
        with self._lock:
            if request := self._historical_requests.get(reqId):
                request.event.set()

    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
        with self._lock:
            if state := self._quote_states.get(reqId):
                state.market_data_type = marketDataType

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: TickAttrib) -> None:  # noqa: N802, ARG002
        if not math.isfinite(price) or price <= 0:
            return
        value = Decimal(str(price))
        with self._lock:
            state = self._quote_states.get(reqId)
            if state is None:
                return
            if tickType in {1, 66}:
                state.bid = value
            elif tickType in {2, 67}:
                state.ask = value
            elif tickType in {4, 68}:
                state.last = value
            else:
                return
            state.timestamp = datetime.now(UTC)

    def tickString(self, reqId: int, tickType: int, value: str) -> None:  # noqa: N802
        if tickType != 45:
            return
        try:
            timestamp = datetime.fromtimestamp(int(value), tz=UTC)
        except (ValueError, OSError):
            return
        with self._lock:
            if state := self._quote_states.get(reqId):
                state.timestamp = timestamp


class IbkrMarketData:
    """One read-only official-ibapi connection. This class exposes no order methods."""

    def __init__(self, host: str, port: int, client_id: int) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self._client = _IbkrClient()
        self._thread: threading.Thread | None = None
        self._futures_contracts: dict[str, ResolvedContract] = {}
        self._subscribed_request_ids: set[int] = set()

    @property
    def connected(self) -> bool:
        return self._client.isConnected() and self._client.ready.is_set()

    @property
    def last_error(self) -> str | None:
        return self._client.last_error

    def connect(self, timeout: float = 10.0) -> None:
        if self.connected:
            return
        if self._client.isConnected():
            self._client.disconnect()
        self._client = _IbkrClient()
        self._subscribed_request_ids.clear()
        self._client.connect(self.host, self.port, self.client_id)
        if not self._client.isConnected():
            raise IbkrUnavailable("IBKR socket connection was rejected")
        self._thread = threading.Thread(target=self._client.run, name="ibkr-reader", daemon=True)
        self._thread.start()
        if not self._client.ready.wait(timeout):
            self._client.disconnect()
            raise IbkrUnavailable("IBKR API handshake timed out")

    def disconnect(self) -> None:
        if self._client.isConnected():
            for request_id in _QUOTE_REQUEST_IDS.values():
                self._client.cancelMktData(request_id)
            self._client.disconnect()
        self._client.ready.clear()
        self._futures_contracts.clear()
        self._subscribed_request_ids.clear()

    def _require_connection(self) -> None:
        if not self.connected:
            raise IbkrUnavailable("IBKR is disconnected")

    def resolve_contract(self, contract: Contract, timeout: float = 15.0) -> ResolvedContract:
        self._require_connection()
        request_id = self._client.next_id()
        request = _ContractRequest()
        with self._client._lock:
            self._client._contract_requests[request_id] = request
        try:
            self._client.reqContractDetails(request_id, contract)
            if not request.event.wait(timeout):
                self._client.cancelContractDetails(request_id)
                raise IbkrUnavailable("IBKR contract resolution timed out")
            if request.error:
                raise IbkrUnavailable(request.error)
            if len(request.details) != 1:
                raise IbkrUnavailable(f"IBKR returned {len(request.details)} contract matches")
            resolved = request.details[0].contract
            multiplier = _decimal(resolved.multiplier)
            if multiplier is None and str(resolved.secType) == "STK":
                multiplier = Decimal(1)
            if multiplier is None or multiplier <= 0:
                raise IbkrUnavailable("IBKR returned an invalid contract multiplier")
            return ResolvedContract(
                con_id=int(resolved.conId),
                symbol=str(resolved.symbol),
                local_symbol=str(resolved.localSymbol),
                security_type=str(resolved.secType),
                exchange=str(resolved.exchange),
                currency=str(resolved.currency),
                trading_class=str(resolved.tradingClass),
                expiry=str(resolved.lastTradeDateOrContractMonth),
                multiplier=multiplier,
            )
        finally:
            with self._client._lock:
                self._client._contract_requests.pop(request_id, None)

    def resolve_spy(self) -> ResolvedContract:
        contract = Contract()
        contract.symbol = "SPY"
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.primaryExchange = "ARCA"
        contract.currency = "USD"
        return self.resolve_contract(contract)

    def resolve_active_futures(
        self, on_date: date | None = None
    ) -> dict[str, ResolvedContract]:
        expiry = active_futures_expiry(on_date or datetime.now(NEW_YORK).date())
        resolved: dict[str, ResolvedContract] = {}
        for symbol in FUTURES_SYMBOLS:
            contract = Contract()
            contract.symbol = symbol
            contract.secType = "FUT"
            contract.exchange = "CME"
            contract.currency = "USD"
            contract.lastTradeDateOrContractMonth = expiry
            resolved[symbol] = self.resolve_contract(contract)

        validated = _validate_futures_contracts(resolved)
        if {_expiry_month(contract) for contract in validated.values()} != {expiry}:
            raise IbkrUnavailable("IBKR resolved futures contracts outside the requested expiry")
        self._futures_contracts = validated
        return dict(validated)

    def subscribe_futures(
        self, contracts: Mapping[str, ResolvedContract] | None = None
    ) -> None:
        self._require_connection()
        selected = _validate_futures_contracts(
            self._futures_contracts if contracts is None else contracts
        )
        self._futures_contracts = selected
        for symbol in FUTURES_SYMBOLS:
            request_id = _QUOTE_REQUEST_IDS[symbol]
            if request_id in self._subscribed_request_ids:
                self._client.cancelMktData(request_id)
            self._client.reset_quote(request_id)
        self._client.reqMarketDataType(1)
        for symbol in FUTURES_SYMBOLS:
            request_id = _QUOTE_REQUEST_IDS[symbol]
            contract = selected[symbol]
            ib_contract = Contract()
            ib_contract.conId = contract.con_id
            ib_contract.symbol = contract.symbol
            ib_contract.secType = contract.security_type
            ib_contract.exchange = contract.exchange
            ib_contract.currency = contract.currency
            ib_contract.localSymbol = contract.local_symbol
            ib_contract.tradingClass = contract.trading_class
            ib_contract.lastTradeDateOrContractMonth = contract.expiry
            ib_contract.multiplier = str(contract.multiplier)
            self._client.reqMktData(request_id, ib_contract, "", False, False, [])
            self._subscribed_request_ids.add(request_id)

    def current_futures_quote(self, symbol: str) -> FuturesQuote:
        self._require_connection()
        normalized_symbol = symbol.upper()
        try:
            contract = self._futures_contracts[normalized_symbol]
            request_id = _QUOTE_REQUEST_IDS[normalized_symbol]
        except KeyError as exc:
            if normalized_symbol not in FUTURES_SYMBOLS:
                raise ValueError(f"unsupported futures symbol: {symbol}") from exc
            raise IbkrUnavailable(f"{normalized_symbol} contract is unresolved") from exc
        state = self._client.quote_state(request_id)
        if state.timestamp is None:
            raise IbkrUnavailable(f"{normalized_symbol} quote is unavailable")
        return FuturesQuote(
            contract=contract,
            bid=state.bid,
            ask=state.ask,
            last=state.last,
            timestamp=state.timestamp,
            market_data_type=state.market_data_type,
        )

    def current_futures_quotes(self) -> dict[str, FuturesQuote]:
        return {symbol: self.current_futures_quote(symbol) for symbol in FUTURES_SYMBOLS}

    def fetch_adjusted_spy_daily_bars(
        self, *, timeout: float = 45.0
    ) -> list[AdjustedSpyDailyBar]:
        self._require_connection()
        spy = self.resolve_spy()
        contract = Contract()
        contract.conId = spy.con_id
        contract.symbol = spy.symbol
        contract.secType = spy.security_type
        contract.exchange = spy.exchange
        contract.currency = spy.currency
        request_id = self._client.next_id()
        request = _HistoricalRequest()
        with self._client._lock:
            self._client._historical_requests[request_id] = request
        try:
            self._client.reqHistoricalData(
                request_id,
                contract,
                "",
                "40 Y",
                "1 day",
                "ADJUSTED_LAST",
                1,
                2,
                False,
                [],
            )
            if not request.event.wait(timeout):
                self._client.cancelHistoricalData(request_id)
                raise IbkrUnavailable("SPY historical request timed out")
            if request.error:
                raise IbkrUnavailable(request.error)
            deduped = {bar.session_date: bar for bar in request.bars}
            return [deduped[key] for key in sorted(deduped)]
        finally:
            with self._client._lock:
                self._client._historical_requests.pop(request_id, None)

    def wait_for_live_quotes(self, timeout: float = 15.0) -> dict[str, FuturesQuote]:
        deadline = time.monotonic() + timeout
        last_problem = "ES and MES quotes are unavailable"
        while time.monotonic() < deadline:
            try:
                quotes = self.current_futures_quotes()
            except IbkrUnavailable as exc:
                last_problem = str(exc)
            else:
                unavailable = [
                    symbol
                    for symbol, quote in quotes.items()
                    if not quote.is_live or quote.mark is None
                ]
                if not unavailable:
                    return quotes
                last_problem = f"{', '.join(unavailable)} market data is not live"
            time.sleep(0.1)
        raise IbkrUnavailable(last_problem)
