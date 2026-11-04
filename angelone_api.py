"""Angel One SmartAPI wrapper with paper trading and live broker access."""

from __future__ import annotations

import json
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

import pyotp
import requests
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from config import CONFIG
from logger import get_logger
from utils import retry_with_backoff

_log = get_logger("angelone_api")


class AngelOneAuthError(RuntimeError):
    pass


class AngelOneAPI:
    def __init__(self) -> None:
        self.kite = SmartConnect(
            api_key=CONFIG.angelone.api_key,
            timeout=CONFIG.angelone.request_timeout_sec,
        )
        self._ticker: Optional[SmartWebSocketV2] = None
        self._subscribed_tokens: set[int] = set()
        self._token_exchange: dict[str, int] = {}
        self._token_symbol: dict[str, str] = {}
        self._last_volume: dict[str, int] = {}
        self._instrument_by_symbol: dict[str, dict] = {}
        self._instruments_cache: Optional[list[dict]] = None
        self._tick_callback: Optional[Callable] = None
        self._ws_lock = threading.Lock()
        self._ws_connected = threading.Event()
        self._ws_stop = threading.Event()
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_correlation_id = "niftybot"
        self._client_code = CONFIG.angelone.client_code
        self._feed_token = ""
        self._refresh_token = ""
        self._paper_mode = CONFIG.mode.value == "PAPER"
        self._paper_orders: dict[str, dict] = {}
        self._paper_positions: dict[str, int] = {}
        self._paper_equity: float = 0.0
        self._paper_initial_equity: float = 0.0
        self._paper_fill_ratio: float = 1.0

    # ------------------------------------------------------------
    # AUTH
    # ------------------------------------------------------------

    def login(self) -> None:
        """
        Reuses today's Angel One session or logs in using client code,
        password/PIN, and a TOTP secret.
        """
        _log.info("MODE=%s | DATA=Real Angel One Live | ORDERS=%s", CONFIG.mode.value, "SIMULATED (Paper)" if self._paper_mode else "REAL (Broker)")
        cached = self._load_cached_token()
        if cached:
            try:
                self.kite.setAccessToken(cached["jwt_token"])
                self._refresh_token = cached["refresh_token"]
                self._feed_token = cached["feed_token"]
                profile = self.kite.getProfile(self._refresh_token)
                self._validate_response(profile, "profile")
                _log.info("Using Cached Token")
                _log.info("Authentication Successful")
                return
            except Exception:
                _log.info("Cached Angel One token expired - requesting new login")

        missing = [
            name for name, value in (
                ("ANGELONE_API_KEY", CONFIG.angelone.api_key),
                ("ANGELONE_CLIENT_CODE", CONFIG.angelone.client_code),
                ("ANGELONE_PASSWORD", CONFIG.angelone.password),
                ("ANGELONE_TOTP_SECRET", CONFIG.angelone.totp_secret),
            ) if not value
        ]
        if missing:
            raise AngelOneAuthError(
                "Missing required Angel One credentials: " + ", ".join(missing)
            )
        try:
            totp = pyotp.TOTP(CONFIG.angelone.totp_secret).now()
            response = self.kite.generateSession(
                CONFIG.angelone.client_code, CONFIG.angelone.password, totp
            )
            session = self._validate_response(response, "login")
        except Exception as exc:
            raise AngelOneAuthError(f"Angel One login failed: {exc}") from exc

        data = session.get("data") or {}
        jwt_token = data.get("jwtToken")
        self._refresh_token = data.get("refreshToken", "")
        self._feed_token = self.kite.getfeedToken()
        if not jwt_token or not self._refresh_token or not self._feed_token:
            raise AngelOneAuthError("Angel One login response did not include all session tokens")
        self.kite.setAccessToken(jwt_token)
        self._save_cached_token(jwt_token, self._refresh_token, self._feed_token)
        _log.info("Authentication Successful")

    @staticmethod
    def _validate_response(response: dict, operation: str) -> dict:
        if not isinstance(response, dict) or not response.get("status"):
            message = response.get("message", "unknown SmartAPI error") if isinstance(response, dict) else "invalid response"
            raise RuntimeError(f"Angel One {operation} failed: {message}")
        return response

    def _tokens_path(self) -> Path:
        return CONFIG.angelone.tokens_path

    def _load_cached_token(self) -> Optional[dict]:
        path = self._tokens_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            if data.get("login_date") != date.today().isoformat():
                return None
            if not all(data.get(key) for key in ("jwt_token", "refresh_token", "feed_token")):
                return None
            return data
        except (OSError, json.JSONDecodeError):
            return None

    def _save_cached_token(self, jwt_token: str, refresh_token: str, feed_token: str) -> None:
        path = self._tokens_path()
        data = {
            "jwt_token": jwt_token,
            "refresh_token": refresh_token,
            "feed_token": feed_token,
            "login_date": date.today().isoformat(),
            "generated_at": datetime.now().isoformat(),
        }
        path.write_text(json.dumps(data, indent=2))
        path.chmod(0o600)
        _log.info("Token Saved")

    def _simulate_fill(self, order: dict) -> tuple[int, float]:
        order_type = order.get("order_type", "LIMIT")
        requested = order.get("quantity", 0)
        price = order.get("price", 0.0)
        trigger = order.get("trigger_price", 0.0)
        if requested <= 0:
            return 0, 0.0
        if order_type == "MARKET":
            return requested, price if price > 0 else 0.0
        if order_type == "SL-M":
            return requested, trigger if trigger > 0 else price
        if order_type == "SL":
            return requested, price if price > 0 else 0.0
        fill_ratio = getattr(self, "_paper_fill_ratio", 1.0)
        if fill_ratio >= 1.0:
            return requested, price
        fill_qty = max(1, int(requested * fill_ratio))
        return fill_qty, price

    def _apply_fill_to_position(self, symbol: str, transaction_type: str, qty: int) -> None:
        current = self._paper_positions.get(symbol, 0)
        if transaction_type == "BUY":
            self._paper_positions[symbol] = current + qty
        else:
            self._paper_positions[symbol] = current - qty

    def process_paper_orders(self, symbol: str, ltp: float) -> None:
        if not self._paper_mode:
            return
        for order_id, order in list(self._paper_orders.items()):
            if order.get("tradingsymbol") != symbol:
                continue
            status = order.get("status", "")
            if status in ("CANCELLED", "REJECTED"):
                continue
            order_type = order.get("order_type", "")
            if order_type not in ("LIMIT", "MARKET", "SL", "SL-M"):
                continue
            txn = order.get("transaction_type", "BUY")
            filled_qty = order.get("filled_quantity", 0)
            if filled_qty > 0 and status == "COMPLETE":
                current_qty = self._paper_positions.get(symbol, 0)
                if txn == "SELL" and current_qty >= 0:
                    self._cancel_opposite_protective_orders(symbol, "SELL")
                elif txn == "BUY" and current_qty <= 0:
                    self._cancel_opposite_protective_orders(symbol, "BUY")

        for order_id, order in list(self._paper_orders.items()):
            if order.get("tradingsymbol") != symbol:
                continue
            status = order.get("status", "")
            if status in ("COMPLETE", "FILLED", "CANCELLED", "REJECTED"):
                continue
            order_type = order.get("order_type", "")
            trigger = order.get("trigger_price", 0.0)
            limit = order.get("price", 0.0)
            requested = order.get("quantity", 0)
            remaining = order.get("remaining_quantity", requested)
            txn = order.get("transaction_type", "BUY")

            if order_type == "SL" and status == "TRIGGER PENDING":
                if (txn == "SELL" and ltp <= trigger) or (txn == "BUY" and ltp >= trigger):
                    fill_qty, fill_price = self._simulate_fill(order)
                    fill_qty = min(fill_qty, remaining)
                    if fill_qty > 0:
                        order["filled_quantity"] = order.get("filled_quantity", 0) + fill_qty
                        order["remaining_quantity"] = remaining - fill_qty
                        order["average_price"] = fill_price
                        order["status"] = "COMPLETE" if order["remaining_quantity"] == 0 else "PARTIAL"
                        self._apply_fill_to_position(symbol, txn, fill_qty)
                        _log.info("PAPER SL filled: symbol=%s order_id=%s filled=%d remaining=%d price=%.2f", symbol, order_id, fill_qty, remaining - fill_qty, fill_price)
                        self._cancel_opposite_protective_orders(symbol, txn)
            elif order_type == "SL-M" and status == "TRIGGER PENDING":
                if (txn == "SELL" and ltp <= trigger) or (txn == "BUY" and ltp >= trigger):
                    fill_qty, fill_price = self._simulate_fill(order)
                    fill_qty = min(fill_qty, remaining)
                    if fill_qty > 0:
                        order["filled_quantity"] = order.get("filled_quantity", 0) + fill_qty
                        order["remaining_quantity"] = remaining - fill_qty
                        order["average_price"] = fill_price
                        order["status"] = "COMPLETE" if order["remaining_quantity"] == 0 else "PARTIAL"
                        self._apply_fill_to_position(symbol, txn, fill_qty)
                        _log.info("PAPER SL-M filled: symbol=%s order_id=%s filled=%d remaining=%d price=%.2f", symbol, order_id, fill_qty, remaining - fill_qty, fill_price)
                        self._cancel_opposite_protective_orders(symbol, txn)
            elif order_type == "LIMIT" and status in ("OPEN", "PARTIAL"):
                current_qty = self._paper_positions.get(symbol, 0)
                if txn == "SELL" and current_qty > 0 and ltp <= limit:
                    fill_qty, fill_price = self._simulate_fill(order)
                    fill_qty = min(fill_qty, remaining, current_qty)
                    if fill_qty > 0:
                        order["filled_quantity"] = order.get("filled_quantity", 0) + fill_qty
                        order["remaining_quantity"] = remaining - fill_qty
                        order["average_price"] = fill_price
                        order["status"] = "COMPLETE" if order["remaining_quantity"] == 0 else "PARTIAL"
                        self._apply_fill_to_position(symbol, txn, fill_qty)
                        _log.info("PAPER LIMIT exit filled: symbol=%s order_id=%s filled=%d remaining=%d price=%.2f", symbol, order_id, fill_qty, remaining - fill_qty, fill_price)
                        self._cancel_opposite_protective_orders(symbol, txn)
                elif txn == "BUY" and current_qty < 0 and ltp >= limit:
                    fill_qty, fill_price = self._simulate_fill(order)
                    fill_qty = min(fill_qty, remaining, abs(current_qty))
                    if fill_qty > 0:
                        order["filled_quantity"] = order.get("filled_quantity", 0) + fill_qty
                        order["remaining_quantity"] = remaining - fill_qty
                        order["average_price"] = fill_price
                        order["status"] = "COMPLETE" if order["remaining_quantity"] == 0 else "PARTIAL"
                        self._apply_fill_to_position(symbol, txn, fill_qty)
                        _log.info("PAPER LIMIT exit filled: symbol=%s order_id=%s filled=%d remaining=%d price=%.2f", symbol, order_id, fill_qty, remaining - fill_qty, fill_price)
                        self._cancel_opposite_protective_orders(symbol, txn)
            elif order_type == "MARKET" and status in ("OPEN", "PARTIAL"):
                fill_qty, fill_price = self._simulate_fill(order)
                fill_qty = min(fill_qty, remaining)
                if fill_qty > 0:
                    order["filled_quantity"] = order.get("filled_quantity", 0) + fill_qty
                    order["remaining_quantity"] = remaining - fill_qty
                    order["average_price"] = fill_price
                    order["status"] = "COMPLETE" if order["remaining_quantity"] == 0 else "PARTIAL"
                    self._apply_fill_to_position(symbol, txn, fill_qty)
                    _log.info("PAPER MARKET exit filled: symbol=%s order_id=%s filled=%d remaining=%d price=%.2f", symbol, order_id, fill_qty, remaining - fill_qty, fill_price)
                    self._cancel_opposite_protective_orders(symbol, txn)

    def _cancel_opposite_protective_orders(self, symbol: str, filled_txn: str) -> None:
        opposite = "BUY" if filled_txn == "SELL" else "SELL"
        for order_id, order in list(self._paper_orders.items()):
            if order.get("tradingsymbol") != symbol:
                continue
            if order.get("transaction_type") == opposite and order.get("order_type") == "SL" and order.get("status") == "TRIGGER PENDING":
                order["status"] = "CANCELLED"
                _log.info("PAPER SL cancelled after exit fill: symbol=%s order_id=%s", symbol, order_id)

    def fill_sl_order(self, symbol: str, fill_price: float) -> None:
        if not self._paper_mode:
            return
        for order_id, order in list(self._paper_orders.items()):
            if order.get("tradingsymbol") != symbol:
                continue
            if order.get("order_type") != "SL":
                continue
            status = order.get("status", "")
            if status not in ("TRIGGER PENDING", "OPEN"):
                continue
            requested = order.get("quantity", 0)
            remaining = order.get("remaining_quantity", requested)
            txn = order.get("transaction_type", "BUY")
            fill_qty, _ = self._simulate_fill(order)
            fill_qty = min(fill_qty, remaining)
            if fill_qty > 0:
                order["filled_quantity"] = order.get("filled_quantity", 0) + fill_qty
                order["remaining_quantity"] = remaining - fill_qty
                order["average_price"] = fill_price
                order["status"] = "COMPLETE" if order["remaining_quantity"] == 0 else "PARTIAL"
                self._apply_fill_to_position(symbol, txn, fill_qty)
                _log.info("PAPER SL force-filled: symbol=%s order_id=%s filled=%d remaining=%d price=%.2f", symbol, order_id, fill_qty, remaining - fill_qty, fill_price)

    def _paper_margin_snapshot(self) -> dict:
        return {
            "paper_initial_equity": self._paper_initial_equity,
            "paper_current_equity": self._paper_equity,
            "paper_available_margin": self._paper_equity,
        }

    def set_paper_equity(self, equity: float) -> None:
        self._paper_equity = float(equity)
        if self._paper_initial_equity == 0.0:
            self._paper_initial_equity = self._paper_equity

    def set_paper_fill_ratio(self, ratio: float) -> None:
        self._paper_fill_ratio = max(0.0, min(1.0, float(ratio)))

    def credit_paper_pnl(self, pnl: float) -> None:
        self._paper_equity += pnl

    def profile(self) -> dict:
        if not self._refresh_token:
            raise AngelOneAuthError("Angel One session is not authenticated")
        response = self._validate_response(self.kite.getProfile(self._refresh_token), "profile")
        return response.get("data") or {}

    # ------------------------------------------------------------
    # REST wrappers (retry-protected)
    # ------------------------------------------------------------

    @retry_with_backoff()
    def margins(self) -> dict:
        if self._paper_mode:
            return {
                "equity": {
                    "available": {
                        "live_balance": self._paper_equity,
                    }
                },
                "paper_equity": self._paper_equity,
                "paper_initial_equity": self._paper_initial_equity,
            }
        response = self._validate_response(self.kite.rmsLimit(), "margin fetch")
        data = response.get("data") or {}
        available = data.get("availablecash", data.get("net"))
        if available is None:
            raise RuntimeError("Angel One margin response did not include available cash")
        return {"equity": {"available": {"live_balance": float(available)}}}

    @retry_with_backoff()
    def instruments(self, exchange: Optional[str] = None) -> list[dict]:
        if self._instruments_cache is None:
            response = requests.get(CONFIG.angelone.scrip_master_url, timeout=30)
            response.raise_for_status()
            raw_instruments = response.json()
            if not isinstance(raw_instruments, list):
                raise RuntimeError("Angel One scrip master response was not a list")
            self._instruments_cache = [
                normalized for item in raw_instruments
                if (normalized := self._normalize_instrument(item)) is not None
            ]
            self._instrument_by_symbol = {
                f"{item['exchange']}:{item['tradingsymbol']}": item
                for item in self._instruments_cache
            }
            for item in self._instruments_cache:
                token = str(item["instrument_token"])
                self._token_exchange[token] = 1 if item["exchange"] == "NSE" else 2
                self._token_symbol[token] = item["tradingsymbol"]
        if not exchange:
            return list(self._instruments_cache)
        exchange = "NFO" if exchange == "NFO" else exchange
        return [item for item in self._instruments_cache if item["exchange"] == exchange]

    @staticmethod
    def _normalize_instrument(item: dict) -> Optional[dict]:
        exchange = item.get("exch_seg")
        symbol = item.get("symbol")
        token = item.get("token")
        instrument_type = item.get("instrumenttype", "")
        if not exchange or not symbol or not token:
            return None
        try:
            token_int = int(token)
        except (TypeError, ValueError):
            return None

        expiry = None
        expiry_raw = item.get("expiry")
        if expiry_raw:
            for fmt in ("%d%b%Y", "%Y-%m-%d"):
                try:
                    expiry = datetime.strptime(expiry_raw, fmt).date()
                    break
                except ValueError:
                    continue

        if instrument_type in ("OPTIDX", "OPTSTK"):
            segment = f"{exchange}-OPT"
            tradingsymbol = symbol
        elif instrument_type in ("FUTIDX", "FUTSTK"):
            segment = f"{exchange}-FUT"
            tradingsymbol = symbol
        elif instrument_type == "AMXIDX":
            segment = "INDICES"
            tradingsymbol = "NIFTY 50" if item.get("name") == "NIFTY" else symbol
        else:
            segment = exchange
            tradingsymbol = symbol

        strike_raw = float(item.get("strike") or 0)
        return {
            "instrument_token": token_int,
            "exchange_token": str(token),
            "tradingsymbol": tradingsymbol,
            "name": item.get("name", ""),
            "last_price": 0.0,
            "expiry": expiry,
            "strike": strike_raw / 100 if instrument_type in ("OPTIDX", "OPTSTK") else strike_raw,
            "tick_size": float(item.get("tick_size") or 0),
            "lot_size": int(item.get("lotsize") or 1),
            "instrument_type": symbol[-2:] if instrument_type in ("OPTIDX", "OPTSTK") else instrument_type,
            "segment": segment,
            "exchange": exchange,
            "symboltoken": str(token),
        }

    @retry_with_backoff()
    def ltp(self, instruments: list[str]) -> dict:
        result = {}
        for instrument in instruments:
            exchange, symbol = instrument.split(":", 1)
            details = self._instrument_by_symbol.get(instrument)
            if details is None:
                self.instruments(exchange)
                details = self._instrument_by_symbol.get(instrument)
            if details is None:
                raise RuntimeError(f"Angel One instrument not found: {instrument}")
            response = self._validate_response(
                self.kite.ltpData(exchange, symbol, str(details["instrument_token"])),
                "LTP fetch",
            )
            data = response.get("data") or {}
            result[instrument] = {"last_price": float(data["ltp"])}
        return result

    @retry_with_backoff()
    def quote(self, instruments: list[str]) -> dict:
        return self.ltp(instruments)

    @retry_with_backoff()
    def historical_data(self, instrument_token: int, from_dt: datetime, to_dt: datetime,
                         interval: str, continuous: bool = False) -> list[dict]:
        token = str(instrument_token)
        exchange_type = self._token_exchange.get(token)
        if exchange_type is None:
            self.instruments()
            exchange_type = self._token_exchange.get(token)
        if exchange_type is None:
            raise RuntimeError(f"Unknown Angel One instrument token: {instrument_token}")
        exchange = "NSE" if exchange_type == 1 else "NFO"
        intervals = {
            "minute": "ONE_MINUTE",
            "3minute": "THREE_MINUTE",
            "5minute": "FIVE_MINUTE",
            "10minute": "TEN_MINUTE",
            "15minute": "FIFTEEN_MINUTE",
            "30minute": "THIRTY_MINUTE",
            "60minute": "ONE_HOUR",
            "day": "ONE_DAY",
        }
        if interval not in intervals:
            raise ValueError(f"Unsupported Angel One candle interval: {interval}")
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": intervals[interval],
            "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        response = self._validate_response(self.kite.getCandleData(params), "historical data fetch")
        candles = response.get("data") or []
        return [
            {
                "date": datetime.fromisoformat(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": int(row[5] or 0),
            }
            for row in candles
        ]

    def _get_instrument(self, exchange: str, symbol: str) -> dict:
        key = f"{exchange}:{symbol}"
        instrument = self._instrument_by_symbol.get(key)
        if instrument is None:
            self.instruments(exchange)
            instrument = self._instrument_by_symbol.get(key)
        if instrument is None:
            raise RuntimeError(f"Angel One instrument not found: {key}")
        return instrument

    @staticmethod
    def _normalize_order(order: dict) -> dict:
        status = str(order.get("orderstatus", order.get("status", "UNKNOWN"))).upper().replace(" ", "")
        status = {
            "COMPLETE": "COMPLETE",
            "FILLED": "COMPLETE",
            "OPEN": "OPEN",
            "PENDING": "OPEN",
            "TRIGGERPENDING": "TRIGGER PENDING",
            "CANCELLED": "CANCELLED",
            "CANCELED": "CANCELLED",
            "REJECTED": "REJECTED",
            "PARTIAL": "PARTIAL",
            "OPENPARTIALLYFILLED": "PARTIAL",
        }.get(status, status)
        raw_type = str(order.get("ordertype", order.get("order_type", ""))).upper()
        order_type = {
            "STOPLOSS_LIMIT": "SL",
            "STOPLOSS_MARKET": "SL-M",
            "SL": "SL",
            "SL-M": "SL-M",
        }.get(raw_type, raw_type)
        return {
            **order,
            "order_id": str(order.get("orderid", order.get("order_id", ""))),
            "tradingsymbol": order.get("tradingsymbol", order.get("symbol", "")),
            "quantity": int(order.get("quantity", 0) or 0),
            "filled_quantity": int(order.get("filledshares", order.get("filled_quantity", 0)) or 0),
            "remaining_quantity": int(order.get("unfilledshares", order.get("remaining_quantity", 0)) or 0),
            "average_price": float(order.get("averageprice", order.get("average_price", 0)) or 0),
            "status": status,
            "transaction_type": str(order.get("transactiontype", order.get("transaction_type", ""))).upper(),
            "order_type": order_type,
            "price": float(order.get("price", 0) or 0),
            "trigger_price": float(order.get("triggerprice", order.get("trigger_price", 0)) or 0),
            "symboltoken": str(order.get("symboltoken", "")),
            "exchange": order.get("exchange", ""),
            "producttype": order.get("producttype", ""),
        }

    def _angel_order_payload(self, kwargs: dict) -> dict:
        exchange = kwargs.get("exchange", "NFO")
        symbol = kwargs["tradingsymbol"]
        instrument = self._get_instrument(exchange, symbol)
        raw_type = kwargs.get("order_type", "LIMIT")
        order_type = {
            "SL": "STOPLOSS_LIMIT",
            "SL-M": "STOPLOSS_MARKET",
        }.get(raw_type, raw_type)
        product = kwargs.get("product", "MIS")
        product_type = {"MIS": "INTRADAY", "NRML": "CARRYFORWARD"}.get(product, product)
        return {
            "variety": kwargs.pop("variety", CONFIG.angelone.variety_regular),
            "tradingsymbol": symbol,
            "symboltoken": str(instrument["instrument_token"]),
            "transactiontype": kwargs.get("transaction_type", "BUY").upper(),
            "exchange": exchange,
            "ordertype": order_type,
            "producttype": product_type,
            "duration": "DAY",
            "price": str(kwargs.get("price", 0) or 0),
            "triggerprice": str(kwargs.get("trigger_price", 0) or 0),
            "quantity": str(kwargs.get("quantity", 0)),
            "squareoff": "0",
            "stoploss": "0",
        }

    @retry_with_backoff(exceptions=(Exception,))
    def place_order(self, **kwargs) -> str:
        if self._paper_mode:
            order_id = "PAPER_" + str(abs(hash(str(kwargs) + str(datetime.now()))))
            tradingsymbol = kwargs.get("tradingsymbol", "UNKNOWN")
            requested_qty = int(kwargs.get("quantity", 0))
            transaction_type = kwargs.get("transaction_type", "BUY")
            order_type = kwargs.get("order_type", "LIMIT")
            price = float(kwargs.get("price", 0.0) or 0.0)
            trigger_price = float(kwargs.get("trigger_price", 0.0) or 0.0)

            order = {
                "order_id": order_id,
                "tradingsymbol": tradingsymbol,
                "quantity": requested_qty,
                "filled_quantity": 0,
                "remaining_quantity": requested_qty,
                "average_price": 0.0,
                "status": "OPEN",
                "transaction_type": transaction_type,
                "order_type": order_type,
                "price": price,
                "trigger_price": trigger_price,
                "placed_at": datetime.now().isoformat(),
            }
            self._paper_orders[order_id] = order

            if order_type == "SL":
                order["status"] = "TRIGGER PENDING"
                order["filled_quantity"] = 0
                order["remaining_quantity"] = requested_qty
            elif order_type == "SL-M":
                order["status"] = "TRIGGER PENDING"
                order["filled_quantity"] = 0
                order["remaining_quantity"] = requested_qty
            elif order_type == "MARKET":
                fill_qty, fill_price = self._simulate_fill(order)
                if fill_qty > 0:
                    order["filled_quantity"] = fill_qty
                    order["remaining_quantity"] = requested_qty - fill_qty
                    order["average_price"] = fill_price
                    order["status"] = "COMPLETE" if fill_qty >= requested_qty else "PARTIAL"
                    self._apply_fill_to_position(tradingsymbol, transaction_type, fill_qty)
            else:
                fill_qty, fill_price = self._simulate_fill(order)
                if fill_qty > 0:
                    order["filled_quantity"] = fill_qty
                    order["remaining_quantity"] = requested_qty - fill_qty
                    order["average_price"] = fill_price
                    order["status"] = "COMPLETE" if fill_qty >= requested_qty else "PARTIAL"
                    self._apply_fill_to_position(tradingsymbol, transaction_type, fill_qty)

            _log.info("PAPER mode: simulated order placed. order_id=%s type=%s symbol=%s requested=%d filled=%d remaining=%d price=%.2f status=%s",
                      order_id, order_type, tradingsymbol, requested_qty, order["filled_quantity"], order["remaining_quantity"], price, order["status"])
            return order_id
        payload = self._angel_order_payload(kwargs)
        response = self._validate_response(self.kite.placeOrderFullResponse(payload), "place order")
        data = response.get("data") or {}
        order_id = data.get("orderid")
        if not order_id:
            raise RuntimeError("Angel One place order response did not include an order id")
        return str(order_id)

    @retry_with_backoff()
    def modify_order(self, variety: str, order_id: str, **kwargs) -> str:
        if self._paper_mode:
            if order_id in self._paper_orders:
                order = self._paper_orders[order_id]
                for key, value in kwargs.items():
                    if key in ("price", "trigger_price"):
                        order[key] = float(value) if value is not None else order.get(key, 0.0)
                    else:
                        order[key] = value
                _log.info("PAPER mode: order modification simulated. order_id=%s new_trigger=%s new_limit=%s",
                          order_id, order.get("trigger_price"), order.get("price"))
            return order_id
        existing = next((o for o in self.orders() if o["order_id"] == str(order_id)), None)
        if existing is None:
            raise RuntimeError(f"Cannot modify missing Angel One order {order_id}")
        merged = {**existing, **kwargs}
        payload = self._angel_order_payload(merged)
        payload["orderid"] = str(order_id)
        response = self._validate_response(self.kite.modifyOrder(payload), "modify order")
        data = response.get("data")
        return str(data.get("orderid", order_id) if isinstance(data, dict) else order_id)

    @retry_with_backoff()
    def cancel_order(self, variety: str, order_id: str) -> str:
        if self._paper_mode:
            if order_id in self._paper_orders:
                self._paper_orders[order_id]["status"] = "CANCELLED"
            _log.info("PAPER mode: order cancellation simulated. order_id=%s", order_id)
            return order_id
        response = self._validate_response(
            self.kite.cancelOrder(str(order_id), variety or CONFIG.angelone.variety_regular),
            "cancel order",
        )
        data = response.get("data")
        if isinstance(data, dict):
            return str(data.get("orderid", order_id))
        return str(order_id)

    @retry_with_backoff()
    def orders(self) -> list[dict]:
        if self._paper_mode:
            return list(self._paper_orders.values())
        response = self._validate_response(self.kite.orderBook(), "order book fetch")
        orders = response.get("data") or []
        return [self._normalize_order(order) for order in orders]

    @retry_with_backoff()
    def order_history(self, order_id: str) -> list[dict]:
        if self._paper_mode:
            order = self._paper_orders.get(order_id)
            if not order:
                return []
            return [dict(order)]
        return [order for order in self.orders() if order["order_id"] == str(order_id)]

    @retry_with_backoff()
    def positions(self) -> dict:
        if self._paper_mode:
            net = []
            for symbol, qty in self._paper_positions.items():
                net.append({"tradingsymbol": symbol, "quantity": qty})
            return {"net": net}
        response = self._validate_response(self.kite.position(), "positions fetch")
        positions = response.get("data") or []
        return {
            "net": [
                {
                    **position,
                    "tradingsymbol": position.get("tradingsymbol", position.get("symbol", "")),
                    "quantity": int(position.get("netqty", position.get("quantity", 0)) or 0),
                }
                for position in positions
            ]
        }

    # ------------------------------------------------------------
    # WEBSOCKET
    # ------------------------------------------------------------

    def start_ticker(self, tokens: list[int], on_tick: Callable[[list[dict]], None]) -> None:
        self._tick_callback = on_tick
        self._subscribed_tokens = set(tokens)
        if not self._token_exchange:
            self.instruments()
        self._ws_stop.clear()
        self._ws_thread = threading.Thread(target=self._run_ticker, daemon=True)
        self._ws_thread.start()

    def _run_ticker(self) -> None:
        while not self._ws_stop.is_set():
            self._ticker = SmartWebSocketV2(
                self.kite.access_token,
                CONFIG.angelone.api_key,
                self._client_code,
                self._feed_token,
            )
            self._ticker.on_open = self._on_connect
            self._ticker.on_close = self._on_close
            self._ticker.on_error = self._on_error
            self._ticker.on_data = self._on_ticks
            try:
                self._ticker.connect()
            except Exception:
                if not self._ws_stop.is_set():
                    _log.exception("Angel One WebSocket connection failed")
            if not self._ws_stop.is_set():
                self._ws_connected.clear()
                self._ws_stop.wait(3)

    def _token_groups(self, tokens: list[int]) -> list[dict]:
        groups: dict[int, list[str]] = {}
        for token in tokens:
            token_text = str(token)
            exchange_type = self._token_exchange.get(token_text)
            if exchange_type is None:
                raise RuntimeError(f"Unknown Angel One WebSocket token: {token}")
            groups.setdefault(exchange_type, []).append(token_text)
        return [
            {"exchangeType": exchange_type, "tokens": token_group}
            for exchange_type, token_group in groups.items()
        ]

    def _on_connect(self, ws, response=None) -> None:
        with self._ws_lock:
            tokens = list(self._subscribed_tokens)
        if tokens:
            ws.subscribe(self._ws_correlation_id, 3, self._token_groups(tokens))
            _log.info("WebSocket connected - subscribed to %d tokens.", len(tokens))
        self._ws_connected.set()

    def _on_close(self, ws, *args) -> None:
        self._ws_connected.clear()
        _log.warning("Angel One WebSocket closed: %s", " ".join(map(str, args)))

    def _on_error(self, ws, error) -> None:
        _log.error("Angel One WebSocket error: %s", error)

    def _on_ticks(self, ws, tick: dict) -> None:
        if not self._tick_callback:
            return
        token = str(tick.get("token", ""))
        cumulative_volume = int(tick.get("volume_trade_for_the_day", 0) or 0)
        previous_volume = self._last_volume.get(token, cumulative_volume)
        self._last_volume[token] = cumulative_volume
        tick_data = {
            **tick,
            "instrument_token": int(token) if token.isdigit() else token,
            "tradingsymbol": self._token_symbol.get(token),
            "last_price": float(tick.get("last_traded_price", 0) or 0) / 100,
            "last_traded_quantity": max(0, cumulative_volume - previous_volume),
        }
        self._tick_callback([tick_data])

    def add_subscription(self, token: int) -> None:
        with self._ws_lock:
            self._subscribed_tokens.add(token)
        if self._ticker and self._ws_connected.is_set():
            self._ticker.subscribe(self._ws_correlation_id, 3, self._token_groups([token]))

    def remove_subscription(self, token: int) -> None:
        with self._ws_lock:
            self._subscribed_tokens.discard(token)
        if self._ticker and self._ws_connected.is_set():
            self._ticker.unsubscribe(
                self._ws_correlation_id,
                3,
                [
                    {"action": 0, **group}
                    for group in self._token_groups([token])
                ],
            )

    def wait_until_connected(self, timeout: float = 15.0) -> bool:
        return self._ws_connected.wait(timeout)

    def stop_ticker(self) -> None:
        self._ws_stop.set()
        if self._ticker:
            self._ticker.close_connection()
        self._ws_connected.clear()
