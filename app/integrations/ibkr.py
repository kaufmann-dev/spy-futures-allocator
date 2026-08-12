from __future__ import annotations

import calendar
import math
import threading
import time
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
class SpyDailyBar:
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class MesQuote:
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
    bars: list[SpyDailyBar] = field(default_factory=list)
    error: str | None = None


def _third_friday(year: int, month: int) -> date:
    month_calendar = calendar.monthcalendar(year, month)
    fridays = [week[calendar.FRIDAY] for week in month_calendar if week[calendar.FRIDAY]]
    return date(year, month, fridays[2])


def active_mes_expiry(on_date: date) -> str:
    """Return YYYYMM for the display contract, rolling eight calendar days before expiry."""

    quarter_months = (3, 6, 9, 12)
    candidates = [(on_date.year, month) for month in quarter_months]
    candidates.extend((on_date.year + 1, month) for month in quarter_months)
    for year, month in candidates:
        expiry = _third_friday(year, month)
        if on_date < expiry - timedelta(days=8):
            return f"{year:04d}{month:02d}"
    raise AssertionError("quarterly MES expiry calculation failed")


def _decimal(value: Any) -> Decimal | None:
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return converted if converted.is_finite() else None


class _IbkrClient(EWrapper, EClient):  # type: ignore[misc]
    QUOTE_REQUEST_ID = 40_001

    def __init__(self) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self._lock = threading.RLock()
        self._request_id = 1_000
        self._contract_requests: dict[int, _ContractRequest] = {}
        self._historical_requests: dict[int, _HistoricalRequest] = {}
        self.market_data_type: int | None = None
        self.bid: Decimal | None = None
        self.ask: Decimal | None = None
        self.last: Decimal | None = None
        self.quote_timestamp: datetime | None = None
        self.last_error: str | None = None

    def next_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

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
            parsed = SpyDailyBar(session_date, open_, high, low, close, volume)
        except (ValueError, TypeError):
            return
        with self._lock:
            if request := self._historical_requests.get(reqId):
                request.bars.append(parsed)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:  # noqa: N802, ARG002
        with self._lock:
            if request := self._historical_requests.get(reqId):
                request.event.set()

    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
        if reqId == self.QUOTE_REQUEST_ID:
            with self._lock:
                self.market_data_type = marketDataType

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: TickAttrib) -> None:  # noqa: N802, ARG002
        if reqId != self.QUOTE_REQUEST_ID or not math.isfinite(price) or price <= 0:
            return
        value = Decimal(str(price))
        with self._lock:
            if tickType in {1, 66}:
                self.bid = value
            elif tickType in {2, 67}:
                self.ask = value
            elif tickType in {4, 68}:
                self.last = value
            else:
                return
            self.quote_timestamp = datetime.now(UTC)

    def tickString(self, reqId: int, tickType: int, value: str) -> None:  # noqa: N802
        if reqId == self.QUOTE_REQUEST_ID and tickType == 45:
            try:
                timestamp = datetime.fromtimestamp(int(value), tz=UTC)
            except (ValueError, OSError):
                return
            with self._lock:
                self.quote_timestamp = timestamp


class IbkrMarketData:
    """One read-only official-ibapi connection. This class exposes no order methods."""

    def __init__(self, host: str, port: int, client_id: int) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self._client = _IbkrClient()
        self._thread: threading.Thread | None = None
        self._mes_contract: ResolvedContract | None = None

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
            self._client.cancelMktData(_IbkrClient.QUOTE_REQUEST_ID)
            self._client.disconnect()
        self._client.ready.clear()
        self._mes_contract = None

    def _require_connection(self) -> None:
        if not self.connected:
            raise IbkrUnavailable("IBKR is disconnected")

    def resolve_contract(self, contract: Contract, timeout: float = 15.0) -> ResolvedContract:
        self._require_connection()
        request_id = self._client.next_id()
        request = _ContractRequest()
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
            self._client._contract_requests.pop(request_id, None)

    def resolve_spy(self) -> ResolvedContract:
        contract = Contract()
        contract.symbol = "SPY"
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.primaryExchange = "ARCA"
        contract.currency = "USD"
        return self.resolve_contract(contract)

    def resolve_active_mes(self, on_date: date | None = None) -> ResolvedContract:
        contract = Contract()
        contract.symbol = "MES"
        contract.secType = "FUT"
        contract.exchange = "CME"
        contract.currency = "USD"
        contract.lastTradeDateOrContractMonth = active_mes_expiry(
            on_date or datetime.now(NEW_YORK).date()
        )
        resolved = self.resolve_contract(contract)
        if resolved.symbol != "MES" or resolved.multiplier != Decimal("5"):
            raise IbkrUnavailable(
                "resolved MES identity or multiplier is not the expected official contract"
            )
        self._mes_contract = resolved
        return resolved

    def subscribe_mes(self, contract: ResolvedContract | None = None) -> None:
        self._require_connection()
        selected = contract or self._mes_contract
        if selected is None:
            raise IbkrUnavailable("MES contract is unresolved")
        ib_contract = Contract()
        ib_contract.conId = selected.con_id
        ib_contract.symbol = selected.symbol
        ib_contract.secType = selected.security_type
        ib_contract.exchange = selected.exchange
        ib_contract.currency = selected.currency
        ib_contract.localSymbol = selected.local_symbol
        ib_contract.tradingClass = selected.trading_class
        ib_contract.lastTradeDateOrContractMonth = selected.expiry
        ib_contract.multiplier = str(selected.multiplier)
        self._client.reqMarketDataType(1)
        self._client.reqMktData(_IbkrClient.QUOTE_REQUEST_ID, ib_contract, "", False, False, [])

    def current_mes_quote(self) -> MesQuote:
        self._require_connection()
        if self._mes_contract is None:
            raise IbkrUnavailable("MES contract is unresolved")
        with self._client._lock:
            timestamp = self._client.quote_timestamp
            if timestamp is None:
                raise IbkrUnavailable("MES quote is unavailable")
            return MesQuote(
                contract=self._mes_contract,
                bid=self._client.bid,
                ask=self._client.ask,
                last=self._client.last,
                timestamp=timestamp,
                market_data_type=self._client.market_data_type,
            )

    def fetch_spy_daily_bars(
        self, *, duration: str = "2 Y", timeout: float = 45.0
    ) -> list[SpyDailyBar]:
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
        self._client._historical_requests[request_id] = request
        try:
            self._client.reqHistoricalData(
                request_id,
                contract,
                "",
                duration,
                "1 day",
                "TRADES",
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
            self._client._historical_requests.pop(request_id, None)

    def wait_for_live_quote(self, timeout: float = 15.0) -> MesQuote:
        deadline = time.monotonic() + timeout
        last_problem = "MES quote is unavailable"
        while time.monotonic() < deadline:
            try:
                quote = self.current_mes_quote()
            except IbkrUnavailable as exc:
                last_problem = str(exc)
            else:
                if quote.is_live and quote.mark is not None:
                    return quote
                last_problem = "MES market data is not live"
            time.sleep(0.1)
        raise IbkrUnavailable(last_problem)
